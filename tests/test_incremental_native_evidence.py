"""Historical native evidence selects an exact existing board through Hermes."""
import os
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'hermes-source-units/src'))
from hermes_source_units import ContractError
from hermes_source_units.incremental_sources import completion_connection


def runtime(tmp_path, monkeypatch, connect):
    home = tmp_path / 'hermes'
    boards = home / 'kanban/boards'
    module = ModuleType('hermes_cli')
    module.kanban_db = SimpleNamespace(
        kanban_home=lambda: home,
        board_dir=lambda board: boards / board,
        board_exists=lambda board: board == 'default' or (boards / board).is_dir())
    module.kanban_db_connect = SimpleNamespace(connect=connect)
    monkeypatch.setitem(sys.modules, 'hermes_cli', module)
    return home, boards


@pytest.mark.parametrize('board', ['source-board', 'default'])
def test_completion_uses_explicit_existing_db_without_changing_worker_pin(tmp_path, monkeypatch, board):
    calls = []
    token = object()
    home, boards = runtime(tmp_path, monkeypatch, lambda **kw: calls.append(kw) or token)
    expected = home / 'kanban.db' if board == 'default' else boards / board / 'kanban.db'
    expected.parent.mkdir(parents=True, exist_ok=True)
    expected.write_bytes(b'isolated existing database')
    pin = str(tmp_path / 'worker-board/kanban.db')
    monkeypatch.setenv('HERMES_KANBAN_DB', pin)
    assert completion_connection(board) is token
    assert calls == [{'db_path': expected.resolve(), 'board': board}]
    assert os.environ['HERMES_KANBAN_DB'] == pin


def test_completion_preserves_hermes_permission_denial(tmp_path, monkeypatch):
    denial = PermissionError('native delegation fence denied this board')
    def denied(**_):
        raise denial
    _, boards = runtime(tmp_path, monkeypatch, denied)
    db = boards / 'source-board/kanban.db'
    db.parent.mkdir(parents=True)
    db.write_bytes(b'isolated existing database')
    with pytest.raises(PermissionError) as caught:
        completion_connection('source-board')
    assert caught.value is denial


def test_completion_never_creates_a_missing_board_or_database(tmp_path, monkeypatch):
    def forbidden(**_):
        pytest.fail('missing native board must not reach connect')
    _, boards = runtime(tmp_path, monkeypatch, forbidden)
    with pytest.raises(ContractError, match='board is missing'):
        completion_connection('missing-board')
    assert not (boards / 'missing-board').exists()
    empty = boards / 'empty-board'
    empty.mkdir(parents=True)
    with pytest.raises(ContractError, match='database is missing'):
        completion_connection('empty-board')
    assert not (empty / 'kanban.db').exists()
