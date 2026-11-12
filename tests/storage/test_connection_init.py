"""Неудачная настройка соединения не оставляет открытый ресурс."""
import gc
import sqlite3
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

from iam.storage import db


class ConnectionInitTest(unittest.TestCase):
    def test_corrupt_file_connection_is_closed_before_error_escapes(self):
        real_connect=sqlite3.connect
        opened=[]
        def tracked(*args,**kwargs):
            con=real_connect(*args,**kwargs);opened.append(con);return con
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'bad.db';path.write_bytes(b'not a sqlite database'*10)
            with patch.object(db.sqlite3,'connect',side_effect=tracked):
                with self.assertRaises(sqlite3.DatabaseError):db.connect(path)
            self.assertEqual(len(opened),1)
            with self.assertRaises(sqlite3.ProgrammingError):opened[0].execute('SELECT 1')

    def test_each_initialization_failure_closes_and_preserves_exception(self):
        for readonly,point in ((False,'PRAGMA busy_timeout = 5000'),(False,'PRAGMA foreign_keys = ON'),
                               (False,'PRAGMA trusted_schema = OFF'),(False,'PRAGMA journal_mode = WAL'),
                               (False,'PRAGMA synchronous = FULL'),(False,'authorizer'),(True,'authorizer')):
            opened=[];failure=RuntimeError('initialization failure')
            class BrokenConnection(db.Connection):
                def __init__(self,*args,**kwargs):
                    super().__init__(*args,**kwargs);opened.append(self)
                def execute(self,sql,*args,**kwargs):
                    if sql==point:raise failure
                    return super().execute(sql,*args,**kwargs)
                def set_authorizer(self,callback):
                    if point=='authorizer':raise failure
                    return super().set_authorizer(callback)
            with self.subTest(readonly=readonly,point=point),tempfile.TemporaryDirectory() as directory:
                path=Path(directory)/'test.db';sqlite3.connect(path).close()
                with patch.object(db,'Connection',BrokenConnection):
                    with self.assertRaises(RuntimeError) as caught:db.connect(path,readonly=readonly)
                self.assertIs(caught.exception,failure)
                with self.assertRaises(sqlite3.ProgrammingError):opened[0].execute('SELECT 1')

    def test_failed_open_does_not_emit_delayed_unclosed_warning(self):
        gc.collect()
        with tempfile.TemporaryDirectory() as directory,warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always',ResourceWarning)
            path=Path(directory)/'bad.db';path.write_bytes(b'not a sqlite database'*10)
            with self.assertRaises(sqlite3.DatabaseError):db.connect(path)
            gc.collect()
            self.assertFalse([w for w in caught if issubclass(w.category,ResourceWarning)])
