"""Fixed package-relative locations; configuration never depends on the shell cwd."""
from pathlib import Path

try:
    from .mcp_common import BridgeError, private_dir
except ImportError:
    from mcp_common import BridgeError, private_dir


class PackageLayout:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.global_data = self.root / 'data/global'
        self.projects = self.root / 'data/projects'
        self.bridge_run = self.root / 'run/bridge'
        self.host_run = self.root / 'run/desktop-host'
        self.index_file = self.global_data / 'project-index.json'
        self.host_state = self.global_data / 'host-state.json'
        self.link_file = self.host_run / 'link.json'
        self.owner_lock = self.bridge_run / 'owner.lock'

    @classmethod
    def from_config(cls, file):
        parent = Path(file).resolve().parent
        return cls(parent.parent if parent.name == 'config' else parent)

    def prepare(self):
        for relative in ['data', 'data/global', 'data/projects', 'run', 'run/bridge', 'run/desktop-host']:
            private_dir(self.root / relative)

    def project_directory(self, project_id):
        if not isinstance(project_id, str) or len(project_id) != 64 or any(c not in '0123456789abcdef' for c in project_id):
            raise BridgeError('Invalid project identity')
        return self.projects / project_id
