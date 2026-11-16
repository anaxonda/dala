from pathlib import Path
import subprocess

import pytest


@pytest.mark.parametrize('directory', ['extension_chrome', 'firefox_extension'])
@pytest.mark.parametrize('namespace', ['menus', 'contextMenus', 'absent'])
def test_context_menu_lifecycle_and_actions(directory, namespace):
    root = Path(__file__).resolve().parents[1]
    subprocess.run(['node', str(root / 'tests/context_menus_extension.cjs'), str(root / directory), namespace], check=True, timeout=20)


def test_browser_context_menu_implementations_match():
    root = Path(__file__).resolve().parents[1]
    helpers = []
    for directory in ['extension_chrome', 'firefox_extension']:
        source = (root / directory / 'background.js').read_text()
        helpers.append(source[source.index('// Keep existing IDs'):source.index('// Command Listener')])
    assert helpers[0] == helpers[1]
