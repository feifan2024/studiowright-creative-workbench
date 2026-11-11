import importlib.util
from pathlib import Path
import sqlite3
import unittest
from test_flow_regressions import fixture_directory


class RetryProjectNames(unittest.TestCase):
    def test_observed_owned_project_is_repaired_without_cross_account_or_custom_rename(self):
        p=Path(__file__).resolve().parents[1]/'packaging/ubuntu/snapshot/flow/console/console_store.py'
        spec=importlib.util.spec_from_file_location('retry_project_store',p)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        opened=[]
        connect=module.Store.connect
        def tracked(instance):
            db=connect(instance);opened.append(db);return db
        module.Store.connect=tracked
        with fixture_directory() as directory:
            (directory/'console').mkdir()
            store=module.Store(directory/'console')
            for profile,email in [('a','alpha@example.invalid'),('b','beta@example.invalid')]:
                store.execute("INSERT INTO accounts(profile,email,state) VALUES(?,?,'verified')",(profile,email))
            store.project('a','owned','未命名项目');store.project('a','custom','My project');store.project('b','other','未命名项目')
            names=directory/'names.sqlite3'
            with sqlite3.connect(names) as db:
                db.execute('CREATE TABLE names(profile,project,title,source)')
                db.executemany('INSERT INTO names VALUES(?,?,?,?)',[
                    ('a','owned','FLOW+alpha','open_flow_tab'),('a','custom','FLOW+alpha','open_flow_tab'),
                    ('a','other','FLOW+alpha','open_flow_tab'),('b','other','FLOW+alpha','open_flow_tab')])
            db.close()
            self.assertEqual(store.sync_observed_project_names(names),1)
            self.assertEqual(store.sync_observed_project_names(names),0)
            self.assertEqual(store.projects('a')[0]['profile'],'a')
            self.assertEqual({r['id']:r['title'] for r in store.projects('a')},dict(owned='FLOW+alpha',custom='My project'))
            self.assertEqual(store.projects('b')[0]['title'],'未命名项目')
            for connection in opened:
                connection.close()
