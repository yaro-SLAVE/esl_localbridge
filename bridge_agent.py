import os
import sys
import json
import time
import threading
import logging
import signal
import socket
import requests
import yaml
import argparse
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, asdict
from pathlib import Path
from queue import Queue, Empty
import hashlib
import hmac
import base64

try:
    import win32serviceutil
    import win32service
    import win32event
    import servicemanager
    WINDOWS_SERVICE = True
except ImportError:
    WINDOWS_SERVICE = False

from flask import Flask, request, jsonify
from flask_cors import CORS
import waitress

@dataclass
class Config:
    bridge_id: str
    cloud_url: str = "https://pricetag.ru"
    api_key: str = ""
    poll_interval: int = 30
    local_api_port: int = 8500
    local_api_host: str = "0.0.0.0"
    esl_gateway_url: str = "http://localhost:8080"
    log_level: str = "INFO"
    auto_update: bool = True
    update_check_interval: int = 3600
    max_retries: int = 3
    retry_delay: int = 5
    
    @classmethod
    def load(cls, config_path: str = None):
        if not config_path:
            if sys.platform == 'win32':
                config_path = Path(os.environ.get('PROGRAMDATA', 'C:/ProgramData')) / 'PriceTag' / 'config.yaml'
            else:
                config_path = Path('/etc/pricetag/config.yaml')
        
        config_path = Path(config_path)
        
        if config_path.exists():
            with open(config_path, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f)
        else:
            data = {}
        
        return cls(
            bridge_id=data.get('bridge_id', ''),
            cloud_url=data.get('cloud_url', 'https://pricetag.ru'),
            api_key=data.get('api_key', ''),
            poll_interval=data.get('poll_interval', 30),
            local_api_port=data.get('local_api_port', 8500),
            local_api_host=data.get('local_api_host', '0.0.0.0'),
            esl_gateway_url=data.get('esl_gateway_url', 'http://localhost:8080'),
            log_level=data.get('log_level', 'INFO'),
            auto_update=data.get('auto_update', True),
            update_check_interval=data.get('update_check_interval', 3600),
            max_retries=data.get('max_retries', 3),
            retry_delay=data.get('retry_delay', 5)
        )
    
    def save(self, config_path: str = None):
        if not config_path:
            if sys.platform == 'win32':
                config_path = Path(os.environ.get('PROGRAMDATA', 'C:/ProgramData')) / 'PriceTag' / 'config.yaml'
            else:
                config_path = Path('/etc/pricetag/config.yaml')
        
        config_path = Path(config_path)
        config_path.parent.mkdir(parents=True, exist_ok=True)
        
        with open(config_path, 'w', encoding='utf-8') as f:
            yaml.dump(asdict(self), f, default_flow_style=False)


@dataclass
class Command:
    id: str
    type: str  # 'update_price', 'reboot', 'ping', 'sync'
    payload: Dict[str, Any]
    created_at: datetime
    expires_at: Optional[datetime] = None
    priority: int = 0


@dataclass
class Device:
    id: str
    mac: str
    type: str
    status: str
    last_seen: datetime
    battery_level: Optional[int] = None
    firmware_version: Optional[str] = None
    current_price: Optional[float] = None
    current_product: Optional[str] = None


@dataclass
class LocalEvent:
    type: str
    device_id: str
    data: Dict[str, Any]
    timestamp: datetime

class LocalApiServer:
    def __init__(self, agent):
        self.agent = agent
        self.app = Flask(__name__)
        CORS(self.app)
        self.setup_routes()
        
    def setup_routes(self):
        @self.app.route('/api/health', methods=['GET'])
        def health():
            return jsonify({
                'status': 'ok',
                'bridge_id': self.agent.config.bridge_id,
                'version': self.agent.version,
                'uptime': self.agent.uptime
            })
        
        @self.app.route('/api/devices', methods=['GET'])
        def list_devices():
            return jsonify({
                'devices': [
                    {
                        'id': d.id,
                        'mac': d.mac,
                        'status': d.status,
                        'battery': d.battery_level,
                        'price': d.current_price
                    }
                    for d in self.agent.devices.values()
                ]
            })
        
        @self.app.route('/api/events', methods=['POST'])
        def receive_event():
            try:
                data = request.json
                event = LocalEvent(
                    type=data.get('type'),
                    device_id=data.get('device_id'),
                    data=data.get('data', {}),
                    timestamp=datetime.fromisoformat(data.get('timestamp', datetime.now().isoformat()))
                )
            
                self.agent.local_events.put(event)
                
                if event.type == 'status':
                    device = self.agent.devices.get(event.device_id)
                    if device:
                        device.status = event.data.get('status', device.status)
                        device.last_seen = event.timestamp
                
                elif event.type == 'battery':
                    device = self.agent.devices.get(event.device_id)
                    if device:
                        device.battery_level = event.data.get('level')
                        device.last_seen = event.timestamp
                
                elif event.type == 'update_confirm':
                    device = self.agent.devices.get(event.device_id)
                    if device:
                        device.current_price = event.data.get('new_price')
                        device.last_seen = event.timestamp
                        
                        self.agent.mark_command_completed(
                            event.data.get('command_id'),
                            'success',
                            event.data
                        )
                
                return jsonify({'status': 'ok', 'received': event.type})
                
            except Exception as e:
                self.agent.logger.error(f"Error processing event: {e}")
                return jsonify({'status': 'error', 'message': str(e)}), 500
        
        @self.app.route('/api/command/<command_id>/status', methods=['PUT'])
        def update_command_status(command_id):
            try:
                data = request.json
                status = data.get('status')
                details = data.get('details', {})
                
                self.agent.mark_command_completed(command_id, status, details)
                
                return jsonify({'status': 'ok'})
                
            except Exception as e:
                return jsonify({'status': 'error', 'message': str(e)}), 500
    
    def start(self):
        def run():
            self.agent.logger.info(f"Starting local API server on {self.agent.config.local_api_host}:{self.agent.config.local_api_port}")
            waitress.serve(
                self.app,
                host=self.agent.config.local_api_host,
                port=self.agent.config.local_api_port,
                threads=4,
                url_scheme='http'
            )
        
        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
    
    def stop(self):
        pass

class PriceTagBridgeAgent:
    def __init__(self, config_path: str = None, debug: bool = False):
        self.version = "1.0.0"
        self.start_time = time.time()
        
        self.config = Config.load(config_path)
        
        self.setup_logging(debug)

        self.running = False
        self.devices: Dict[str, Device] = {}
        self.pending_commands: Dict[str, Command] = {}
        self.completed_commands: Dict[str, Dict] = {}
        
        self.cloud_commands = Queue()
        self.local_events = Queue()
        
        self.local_api = LocalApiServer(self)
        
        self.stats = {
            'commands_received': 0,
            'commands_executed': 0,
            'commands_failed': 0,
            'events_sent': 0,
            'poll_count': 0,
            'last_poll_time': None
        }
        
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': f'PriceTagBridge/{self.version}',
            'Content-Type': 'application/json'
        })
        
        self.last_poll_time = 0
        self.last_update_check = 0
    
    @property
    def uptime(self):
        return int(time.time() - self.start_time)
    
    def setup_logging(self, debug: bool):
        log_level = logging.DEBUG if debug else getattr(logging, self.config.log_level)
        
        log_format = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        
        log_file = None
        if sys.platform == 'win32':
            log_dir = Path(os.environ.get('PROGRAMDATA', 'C:/ProgramData')) / 'PriceTag' / 'logs'
        else:
            log_dir = Path('/var/log/pricetag')
        
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f'bridge_{self.config.bridge_id}.log'
        
        logging.basicConfig(
            level=log_level,
            format=log_format,
            handlers=[
                logging.FileHandler(log_file),
                logging.StreamHandler()
            ]
        )
        
        self.logger = logging.getLogger('PriceTagBridge')
        self.logger.info(f"Bridge Agent v{self.version} starting. Bridge ID: {self.config.bridge_id}")
    
    def sign_request(self, data: Dict) -> Dict:
        if not self.config.api_key:
            return data
        
        timestamp = str(int(time.time()))
        message = timestamp + json.dumps(data, sort_keys=True)
        signature = hmac.new(
            self.config.api_key.encode(),
            message.encode(),
            hashlib.sha256
        ).hexdigest()
        
        return {
            'bridge_id': self.config.bridge_id,
            'timestamp': timestamp,
            'signature': signature,
            'data': data
        }
    
    def poll_cloud(self) -> List[Command]:
        try:
            self.logger.debug("Polling cloud for commands...")
            
            payload = self.sign_request({
                'bridge_id': self.config.bridge_id,
                'status': 'online',
                'stats': {
                    'devices_online': sum(1 for d in self.devices.values() if d.status == 'online'),
                    'pending_commands': len(self.pending_commands),
                    'free_memory': None,
                    'uptime': self.uptime
                }
            })
            
            response = self.session.post(
                f"{self.config.cloud_url}/api/bridge/poll",
                json=payload,
                timeout=30
            )
            
            if response.status_code == 200:
                data = response.json()
                
                if 'config' in data:
                    self.update_config(data['config'])
                
                commands = []
                for cmd_data in data.get('commands', []):
                    command = Command(
                        id=cmd_data['id'],
                        type=cmd_data['type'],
                        payload=cmd_data['payload'],
                        created_at=datetime.fromisoformat(cmd_data.get('created_at', datetime.now().isoformat())),
                        expires_at=datetime.fromisoformat(cmd_data['expires_at']) if cmd_data.get('expires_at') else None,
                        priority=cmd_data.get('priority', 0)
                    )
                    commands.append(command)
                    
                    self.cloud_commands.put(command)
                    self.pending_commands[command.id] = command
                    self.stats['commands_received'] += 1
                
                self.stats['last_poll_time'] = datetime.now()
                self.stats['poll_count'] += 1
                
                self.logger.info(f"Polled cloud: {len(commands)} new commands")
                return commands
                
            else:
                self.logger.error(f"Poll failed: {response.status_code} - {response.text}")
                return []
                
        except requests.exceptions.RequestException as e:
            self.logger.error(f"Poll error: {e}")
            return []
    
    def send_events(self):
        events_to_send = []
        
        while not self.local_events.empty():
            try:
                event = self.local_events.get_nowait()
                events_to_send.append(asdict(event))
            except Empty:
                break
        
        if not events_to_send:
            return
        
        try:
            self.logger.debug(f"Sending {len(events_to_send)} events to cloud")
            
            payload = self.sign_request({
                'bridge_id': self.config.bridge_id,
                'events': events_to_send
            })
            
            response = self.session.post(
                f"{self.config.cloud_url}/api/bridge/events",
                json=payload,
                timeout=30
            )
            
            if response.status_code == 200:
                self.stats['events_sent'] += len(events_to_send)
                self.logger.info(f"Sent {len(events_to_send)} events to cloud")
            else:
                self.logger.error(f"Events send failed: {response.status_code}")
                
                for event in events_to_send:
                    self.local_events.put(event)
                    
        except Exception as e:
            self.logger.error(f"Events send error: {e}")
            for event in events_to_send:
                self.local_events.put(event)
    
    def execute_command(self, command: Command):
        self.logger.info(f"Executing command {command.id}: {command.type}")
        
        try:
            if command.type == 'update_price':
                result = self.execute_update_price(command)
                
            elif command.type == 'sync':
                result = self.execute_sync(command)
                
            elif command.type == 'reboot':
                result = self.execute_reboot(command)
                
            elif command.type == 'ping':
                result = self.execute_ping(command)
                
            elif command.type == 'update_firmware':
                result = self.execute_update_firmware(command)
                
            else:
                self.logger.warning(f"Unknown command type: {command.type}")
                self.mark_command_completed(command.id, 'failed', {'error': 'Unknown command type'})
                return
            
            if result['success']:
                self.mark_command_completed(command.id, 'success', result.get('details', {}))
                self.stats['commands_executed'] += 1
            else:
                self.mark_command_completed(command.id, 'failed', result.get('error', {}))
                self.stats['commands_failed'] += 1
                
        except Exception as e:
            self.logger.error(f"Command execution error: {e}")
            self.mark_command_completed(command.id, 'failed', {'error': str(e)})
            self.stats['commands_failed'] += 1
    
    def execute_update_price(self, command: Command) -> Dict:
        esl_mac = command.payload.get('esl_mac')
        new_price = command.payload.get('new_price')
        product_name = command.payload.get('product_name', '')
        
        if not esl_mac or not new_price:
            return {'success': False, 'error': 'Missing required fields'}
        
        try:
            response = requests.post(
                f"{self.config.esl_gateway_url}/api/v1/display/update",
                json={
                    'mac_address': esl_mac,
                    'price': f"{new_price:.2f}",
                    'line1': product_name[:20],
                    'line2': f"{new_price} ₽",
                    'command_id': command.id
                },
                timeout=10
            )
            
            if response.status_code == 200:
                device = self.devices.get(esl_mac)
                if device:
                    device.current_price = new_price
                
                return {
                    'success': True,
                    'details': {
                        'gateway_response': response.json(),
                        'esl_mac': esl_mac
                    }
                }
            else:
                return {
                    'success': False,
                    'error': f"Gateway returned {response.status_code}: {response.text}"
                }
                
        except requests.exceptions.RequestException as e:
            return {'success': False, 'error': str(e)}
    
    def execute_sync(self, command: Command) -> Dict:
        try:
            response = requests.get(
                f"{self.config.esl_gateway_url}/api/v1/devices",
                timeout=10
            )
            
            if response.status_code == 200:
                devices_data = response.json()
                
                for dev_data in devices_data:
                    device = Device(
                        id=dev_data['id'],
                        mac=dev_data['mac'],
                        type=dev_data.get('type', 'unknown'),
                        status=dev_data.get('status', 'unknown'),
                        last_seen=datetime.now(),
                        battery_level=dev_data.get('battery'),
                        firmware_version=dev_data.get('firmware'),
                        current_price=dev_data.get('price')
                    )
                    self.devices[device.mac] = device
                
                return {
                    'success': True,
                    'details': {
                        'devices_count': len(devices_data),
                        'devices': devices_data
                    }
                }
            else:
                return {'success': False, 'error': f"Sync failed: {response.status_code}"}
                
        except Exception as e:
            return {'success': False, 'error': str(e)}
    
    def execute_reboot(self, command: Command) -> Dict:
        esl_mac = command.payload.get('esl_mac')
        
        if not esl_mac:
            return {'success': False, 'error': 'Missing MAC address'}
        
        try:
            response = requests.post(
                f"{self.config.esl_gateway_url}/api/v1/display/reboot",
                json={'mac_address': esl_mac},
                timeout=10
            )
            
            if response.status_code == 200:
                return {'success': True, 'details': {'esl_mac': esl_mac}}
            else:
                return {'success': False, 'error': f"Reboot failed: {response.status_code}"}
                
        except Exception as e:
            return {'success': False, 'error': str(e)}
    
    def execute_ping(self, command: Command) -> Dict:
        esl_mac = command.payload.get('esl_mac')
        
        try:
            if esl_mac:
                response = requests.get(
                    f"{self.config.esl_gateway_url}/api/v1/device/{esl_mac}/ping",
                    timeout=5
                )
                
                return {
                    'success': response.status_code == 200,
                    'details': {
                        'esl_mac': esl_mac,
                        'status_code': response.status_code
                    }
                }
            else:
                response = requests.get(
                    f"{self.config.esl_gateway_url}/api/v1/health",
                    timeout=5
                )
                
                return {
                    'success': response.status_code == 200,
                    'details': {
                        'gateway_status': response.json() if response.status_code == 200 else None
                    }
                }
                
        except Exception as e:
            return {'success': False, 'error': str(e)}
    
    def execute_update_firmware(self, command: Command) -> Dict:
        esl_mac = command.payload.get('esl_mac')
        firmware_url = command.payload.get('firmware_url')
        
        if not esl_mac or not firmware_url:
            return {'success': False, 'error': 'Missing required fields'}
        
        try:
            firmware_response = requests.get(firmware_url, timeout=30)
            
            if firmware_response.status_code != 200:
                return {'success': False, 'error': 'Failed to download firmware'}
            
            response = requests.post(
                f"{self.config.esl_gateway_url}/api/v1/device/{esl_mac}/firmware",
                files={'firmware': firmware_response.content},
                timeout=60
            )
            
            if response.status_code == 200:
                return {'success': True, 'details': {'esl_mac': esl_mac}}
            else:
                return {'success': False, 'error': f"Firmware update failed: {response.status_code}"}
                
        except Exception as e:
            return {'success': False, 'error': str(e)}
    
    def mark_command_completed(self, command_id: str, status: str, details: Dict = None):
        if command_id in self.pending_commands:
            command = self.pending_commands.pop(command_id)
            
            self.completed_commands[command_id] = {
                'command': asdict(command),
                'status': status,
                'details': details or {},
                'completed_at': datetime.now().isoformat()
            }
            
            self.send_command_status(command_id, status, details)
    
    def send_command_status(self, command_id: str, status: str, details: Dict = None):
        try:
            payload = self.sign_request({
                'bridge_id': self.config.bridge_id,
                'command_id': command_id,
                'status': status,
                'details': details or {}
            })
            
            response = self.session.post(
                f"{self.config.cloud_url}/api/bridge/command-status",
                json=payload,
                timeout=10
            )
            
            if response.status_code != 200:
                self.logger.error(f"Status send failed: {response.status_code}")
                
        except Exception as e:
            self.logger.error(f"Status send error: {e}")
    
    def update_config(self, new_config: Dict):
        updated = False
        
        if 'poll_interval' in new_config and new_config['poll_interval'] != self.config.poll_interval:
            self.config.poll_interval = new_config['poll_interval']
            updated = True
            self.logger.info(f"Poll interval updated to {new_config['poll_interval']}s")
        
        if updated:
            self.config.save()
    
    def check_for_updates(self):
        if not self.config.auto_update:
            return
        
        now = time.time()
        if now - self.last_update_check < self.config.update_check_interval:
            return
        
        self.last_update_check = now
        
        try:
            response = self.session.get(
                f"{self.config.cloud_url}/api/bridge/version",
                params={'current': self.version},
                timeout=10
            )
            
            if response.status_code == 200:
                data = response.json()
                if data.get('update_required'):
                    self.perform_update(data['download_url'])
                    
        except Exception as e:
            self.logger.error(f"Update check failed: {e}")
    
    def perform_update(self, download_url: str):
        self.logger.info(f"Downloading update from {download_url}")
        
        try:
            response = requests.get(download_url, stream=True, timeout=60)
            
            if response.status_code != 200:
                self.logger.error("Failed to download update")
                return
            
            import tempfile
            with tempfile.NamedTemporaryFile(delete=False, suffix='.exe') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
                new_exe = f.name
            
            if sys.platform == 'win32':
                current_exe = sys.executable if getattr(sys, 'frozen', False) else sys.argv[0]
                
                update_script = f'''@echo off
timeout /t 2 /nobreak > nul
copy /y "{new_exe}" "{current_exe}"
del "{new_exe}"
net start PriceTagBridge
'''
                
                with open('update.bat', 'w') as f:
                    f.write(update_script)
                
                os.system('net stop PriceTagBridge')
                
                os.system('start update.bat')
                
                sys.exit(0)
                
        except Exception as e:
            self.logger.error(f"Update failed: {e}")
    
    def run(self):
        self.running = True
        
        self.local_api.start()
        
        self.logger.info(f"Bridge agent started. Local API: http://{self.config.local_api_host}:{self.config.local_api_port}")
        
        while self.running:
            try:
                if time.time() - self.last_poll_time >= self.config.poll_interval:
                    self.poll_cloud()
                    self.last_poll_time = time.time()
                
                self.send_events()
                
                try:
                    command = self.cloud_commands.get_nowait()
                    self.execute_command(command)
                except Empty:
                    pass
                
                self.check_for_updates()
                
                time.sleep(0.1)
                
            except KeyboardInterrupt:
                self.logger.info("Shutting down...")
                self.running = False
                break
                
            except Exception as e:
                self.logger.error(f"Main loop error: {e}")
                time.sleep(5)
    
    def stop(self):
        self.running = False
        self.local_api.stop()
        self.logger.info("Bridge agent stopped")

if WINDOWS_SERVICE:
    class PriceTagBridgeService(win32serviceutil.ServiceFramework):
        _svc_name_ = "PriceTagBridge"
        _svc_display_name_ = "PriceTag ESL Bridge Agent"
        _svc_description_ = "Двусторонний мост между облаком PriceTag и локальными ESL-ценниками"
        
        def __init__(self, args):
            win32serviceutil.ServiceFramework.__init__(self, args)
            self.hWaitStop = win32event.CreateEvent(None, 0, 0, None)
            self.agent = None
            self.running = True
        
        def SvcStop(self):
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            win32event.SetEvent(self.hWaitStop)
            if self.agent:
                self.agent.stop()
            self.running = False
        
        def SvcDoRun(self):
            servicemanager.LogMsg(
                servicemanager.EVENTLOG_INFORMATION_TYPE,
                servicemanager.PYS_SERVICE_STARTED,
                (self._svc_name_, '')
            )
            
            try:
                self.agent = PriceTagBridgeAgent()
                self.agent.run()
                
            except Exception as e:
                servicemanager.LogMsg(
                    servicemanager.EVENTLOG_ERROR_TYPE,
                    servicemanager.PYS_SERVICE_STARTED,
                    (self._svc_name_, str(e))
                )
                raise


def install_windows_service():
    if not WINDOWS_SERVICE:
        print("Windows Service modules not available")
        return
    
    try:
        win32serviceutil.InstallService(
            None,
            PriceTagBridgeService._svc_name_,
            PriceTagBridgeService._svc_display_name_,
            startType=win32service.SERVICE_AUTO_START,
            description=PriceTagBridgeService._svc_description_,
            exeName=f'"{sys.executable}" "{__file__}"'
        )
        print(f"Service {PriceTagBridgeService._svc_name_} installed successfully")
    except Exception as e:
        print(f"Failed to install service: {e}")


def uninstall_windows_service():
    if not WINDOWS_SERVICE:
        print("Windows Service modules not available")
        return
    
    try:
        win32serviceutil.RemoveService(PriceTagBridgeService._svc_name_)
        print(f"Service {PriceTagBridgeService._svc_name_} removed successfully")
    except Exception as e:
        print(f"Failed to remove service: {e}")


def start_windows_service():
    if not WINDOWS_SERVICE:
        return
    
    try:
        win32serviceutil.StartService(PriceTagBridgeService._svc_name_)
        print(f"Service {PriceTagBridgeService._svc_name_} started")
    except Exception as e:
        print(f"Failed to start service: {e}")


def stop_windows_service():
    if not WINDOWS_SERVICE:
        return
    
    try:
        win32serviceutil.StopService(PriceTagBridgeService._svc_name_)
        print(f"Service {PriceTagBridgeService._svc_name_} stopped")
    except Exception as e:
        print(f"Failed to stop service: {e}")

def first_time_setup():
    print("\n" + "="*50)
    print("PriceTag Bridge Agent - Первоначальная настройка")
    print("="*50 + "\n")
    
    bridge_id = input("Введите ID магазина (получен в личном кабинете): ").strip()
    if not bridge_id:
        print("ID магазина обязателен")
        return False
    
    api_key = input("Введите API ключ (получен в личном кабинете): ").strip()
    if not api_key:
        print("API ключ обязателен")
        return False
    
    cloud_url = input("URL облачного сервера [https://pricetag.ru]: ").strip()
    if not cloud_url:
        cloud_url = "https://pricetag.ru"
    
    esl_gateway = input("URL ESL Gateway [http://localhost:8080]: ").strip()
    if not esl_gateway:
        esl_gateway = "http://localhost:8080"
    
    config = Config(
        bridge_id=bridge_id,
        api_key=api_key,
        cloud_url=cloud_url,
        esl_gateway_url=esl_gateway
    )
    
    config.save()
    
    print("\nКонфигурация сохранена")
    print(f"   Конфиг: {Path(os.environ.get('PROGRAMDATA', 'C:/ProgramData')) / 'PriceTag' / 'config.yaml'}")
    
    return True

def main():
    parser = argparse.ArgumentParser(description='PriceTag Bridge Agent')
    parser.add_argument('--install', action='store_true', help='Установить как Windows Service')
    parser.add_argument('--uninstall', action='store_true', help='Удалить Windows Service')
    parser.add_argument('--start', action='store_true', help='Запустить службу')
    parser.add_argument('--stop', action='store_true', help='Остановить службу')
    parser.add_argument('--setup', action='store_true', help='Первоначальная настройка')
    parser.add_argument('--debug', action='store_true', help='Режим отладки (консоль)')
    parser.add_argument('--config', type=str, help='Путь к конфигурационному файлу')
    
    args = parser.parse_args()
    
    if len(sys.argv) == 1 and WINDOWS_SERVICE:
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(PriceTagBridgeService)
        servicemanager.StartServiceCtrlDispatcher()
        return
    
    if args.install:
        install_windows_service()
        first_time_setup()
        start_windows_service()
        
    elif args.uninstall:
        stop_windows_service()
        uninstall_windows_service()
        
    elif args.start:
        start_windows_service()
        
    elif args.stop:
        stop_windows_service()
        
    elif args.setup:
        first_time_setup()
        
    else:
        if args.debug:
            print("DEBUG MODE - Running in console")
        
        config = Config.load(args.config)
        if not config.bridge_id:
            print("Конфигурация не найдена. Запустите с --setup")
            return
        
        agent = PriceTagBridgeAgent(args.config, debug=args.debug)
        try:
            agent.run()
        except KeyboardInterrupt:
            print("\nShutting down...")
            agent.stop()


if __name__ == '__main__':
    main()