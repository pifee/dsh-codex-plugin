"""One writer, indexed project files; each job and its notification share one commit."""
import hashlib
import json
from pathlib import Path

try:
    from .mcp_common import BridgeError, atomic_json, private_dir
except ImportError:
    from mcp_common import BridgeError, atomic_json, private_dir


def stable_id(key):
    return hashlib.sha256(key.encode('utf8')).hexdigest()


def project_key(cwd, parent):
    if isinstance(cwd, str) and cwd:
        if not Path(cwd).is_absolute(): raise BridgeError('Absolute project directory required')
        return 'cwd:' + str(Path(cwd).resolve())
    if not isinstance(parent, str) or not parent: raise BridgeError('Project parent identity required')
    return 'parent:' + parent


def read_json(file):
    if file.is_symlink(): raise BridgeError('Physical state file required')
    return json.loads(file.read_text())


class ProjectStore:
    def __init__(self, layout):
        self.layout = layout
        self.index = read_json(layout.index_file) if layout.index_file.exists() else {
            'schema_version': 1, 'projects': {}, 'sessions': {}, 'creations': {}}
        if self.index.get('schema_version') != 1: raise BridgeError('Unsupported project storage version')
        self.last_saved = {}
        self.last_saved[layout.index_file] = self.encoded(self.index)
        self.state = {'sessions': {}, 'creations': self.index['creations']}
        for project_id in self.index['projects']:
            file = self.project_file(project_id)
            if not file.exists(): raise BridgeError('Project state is missing')
            value = read_json(file)
            if value.get('project_id') != project_id: raise BridgeError('Project state identity mismatch')
            for sid, session in value.get('sessions', {}).items():
                if sid in self.state['sessions']: raise BridgeError('Duplicate stored conversation identity')
                self.state['sessions'][sid] = session
                self.index['sessions'][sid] = project_id
            self.last_saved[file] = self.encoded(value)

    @staticmethod
    def encoded(value): return json.dumps(value, ensure_ascii=False, sort_keys=True)

    def project_file(self, project_id): return self.layout.project_directory(project_id) / 'state.json'

    def register(self, cwd, parent, preferred_id=None):
        key = project_key(cwd, parent)
        existing = next((pid for pid, row in self.index['projects'].items() if row['key'] == key), None)
        if existing:
            if preferred_id is not None and preferred_id != existing: raise BridgeError('Project identity mismatch')
            return existing
        project_id = preferred_id or stable_id(key)
        self.layout.project_directory(project_id)
        prior = self.index['projects'].get(project_id)
        if prior and prior['key'] != key: raise BridgeError('Project identity mismatch')
        self.index['projects'][project_id] = {'key': key, 'cwd': key[4:] if key.startswith('cwd:') else None}
        return project_id

    def save(self):
        grouped = {}
        for sid, session in self.state['sessions'].items():
            project_id = self.index['sessions'].get(sid)
            if project_id is None:
                project_id = self.register(session.get('cwd'), session.get('parent'), session.get('projectId'))
                self.index['sessions'][sid] = project_id
            grouped.setdefault(project_id, {})[sid] = session
        # Project files commit first. Result/state/notification are inseparable;
        # the global index contains only ownership pointers and creation intents.
        for project_id in self.index['projects']:
            value = {'schema_version': 1, 'project_id': project_id, 'sessions': grouped.get(project_id, {})}
            directory = self.layout.project_directory(project_id); private_dir(directory)
            self.write_changed(directory / 'state.json', value)
        self.index['creations'] = self.state.get('creations', {})
        self.write_changed(self.layout.index_file, self.index)

    def write_changed(self, file, value):
        encoded = self.encoded(value)
        if self.last_saved.get(file) == encoded and file.exists(): return
        atomic_json(file, value); self.last_saved[file] = encoded
