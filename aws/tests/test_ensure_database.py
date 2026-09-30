"""Initialization never overwrites a schema; locking covers its validation."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

spec = importlib.util.spec_from_file_location('ensure_database', Path(__file__).parents[1] / 'scripts' / 'ensure_database.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class EnsureDatabaseTests(unittest.TestCase):
    def setup_db(self, initial):
        db = MagicMock()
        table_queries = iter([initial, [(name,) for name in module.EXPECTED_TABLES]])
        def execute(query, *args):
            result = MagicMock()
            if 'pg_tables' in query:
                result.fetchall.return_value = next(table_queries)
            return result
        db.execute.side_effect = execute
        return db

    def test_empty_schema_created_under_lock(self):
        db = self.setup_db([])
        with patch.object(module.psycopg, 'connect') as connect, patch.object(module, 'PostgresUIDStore') as store:
            connect.return_value.__enter__.return_value = db
            module.ensure_database('private-dsn')
        queries = [call.args[0] for call in db.execute.call_args_list]
        self.assertLess(queries.index('SELECT pg_advisory_lock(%s)'), queries.index(module.SCHEMA))
        self.assertEqual(queries[-1], 'SELECT pg_advisory_unlock(%s)')
        store.return_value.close.assert_called_once()

    def test_existing_schema_never_reinitialized(self):
        db = self.setup_db([('settings',)])
        with patch.object(module.psycopg, 'connect') as connect, patch.object(module, 'PostgresUIDStore'):
            connect.return_value.__enter__.return_value = db
            module.ensure_database('private-dsn')
        self.assertNotIn(module.SCHEMA, [call.args[0] for call in db.execute.call_args_list])

    def test_partial_schema_rejected_and_lock_released(self):
        db = MagicMock()
        db.execute.return_value.fetchall.return_value = [('settings',)]
        with patch.object(module.psycopg, 'connect') as connect, patch.object(module, 'PostgresUIDStore') as store:
            connect.return_value.__enter__.return_value = db
            with self.assertRaises(ValueError):
                module.ensure_database('private-dsn')
        store.assert_not_called()
        self.assertEqual(db.execute.call_args_list[-1].args[0], 'SELECT pg_advisory_unlock(%s)')

    def test_invalid_schema_fails_without_reset(self):
        db = self.setup_db([('settings',)])
        with patch.object(module.psycopg, 'connect') as connect, patch.object(module, 'PostgresUIDStore', side_effect=ValueError('invalid')):
            connect.return_value.__enter__.return_value = db
            with self.assertRaises(ValueError):
                module.ensure_database('private-dsn')
        self.assertNotIn(module.SCHEMA, [call.args[0] for call in db.execute.call_args_list])
        self.assertEqual(db.execute.call_args_list[-1].args[0], 'SELECT pg_advisory_unlock(%s)')
