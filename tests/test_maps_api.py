"""配置接口使用内存数据库和隔离配置，不访问用户密钥文件。"""
import importlib
import sqlite3
import sys
from pathlib import Path
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
import config
import db
import tools

class MapsAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn=sqlite3.connect(':memory:',check_same_thread=False);cls.conn.row_factory=sqlite3.Row
        cls.context=dict(tools._ctx)
        with patch('db.get_conn',return_value=cls.conn):
            cls.main=importlib.import_module('main')
        cls.client=TestClient(cls.main.app)
    @classmethod
    def tearDownClass(cls):
        cls.client.close();cls.conn.close();tools._ctx.update(cls.context)
    def setUp(self):
        self.cfg={**config.DEFAULT_CONFIG,'api_key':'isolated-deepseek-key','amap_key':'isolated-map-secret'}
        self.saved=[]
        self.patches=[patch('config.load_config',side_effect=lambda:dict(self.cfg)),patch('config.save_config',side_effect=self.save)]
        for p in self.patches:p.start()
    def save(self,cfg):
        self.saved.append(cfg);self.cfg=dict(cfg)
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
    def test_read_masks_key_and_has_no_provider_secret(self):
        response=self.client.get('/api/maps/config')
        self.assertEqual(response.status_code,200)
        self.assertTrue(response.json()['has_key'])
        self.assertNotIn('isolated-map-secret',response.text)
        self.assertNotIn('isolated-deepseek-key',response.text)
    def test_patch_preserves_existing_keys_and_invalid_patch_does_not_save(self):
        response=self.client.post('/api/maps/config',json={'city':'大庆市','radius':4500})
        self.assertEqual(response.status_code,200)
        self.assertEqual(self.saved[0]['amap_key'],'isolated-map-secret')
        self.assertEqual(self.saved[0]['api_key'],'isolated-deepseek-key')
        for body in [{'campus_location':'999,0'},{'radius':50001}]:
            self.assertEqual(self.client.post('/api/maps/config',json=body).status_code,400)
        self.assertEqual(len(self.saved),1)
    def test_missing_key_test_has_useful_error_and_search_does_not_fabricate_results(self):
        self.cfg['amap_key']=''
        with patch('maps.httpx.Client') as network:
            response=self.client.post('/api/maps/test')
            self.assertEqual(response.status_code,400)
            self.assertIn('配置',response.json()['error'])
            search=self.client.post('/api/maps/search',json={'query':'餐馆'})
            self.assertEqual(search.status_code,200)
            self.assertEqual(search.json()['results'],[])
            self.assertTrue(search.json()['config_needed'])
            network.assert_not_called()

    def test_school_template_lists_directory_and_switch_preserves_provider_keys(self):
        directory = self.client.get('/api/schools')
        self.assertEqual(directory.status_code, 200)
        self.assertGreaterEqual(directory.json()['count'], 80)
        switched = self.client.post('/api/schools/select', json={'school_id': 'hlj-001'})
        self.assertEqual(switched.status_code, 200)
        self.assertEqual(self.saved[-1]['current_school_id'], 'hlj-001')
        self.assertEqual(self.saved[-1]['api_key'], 'isolated-deepseek-key')
        self.assertEqual(self.saved[-1]['amap_key'], 'isolated-map-secret')

    def test_school_template_accepts_a_custom_school_without_exposing_keys(self):
        response = self.client.post('/api/schools', json={'name': '测试职业学院', 'city': '哈尔滨市'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['school']['name'], '测试职业学院')
        self.assertNotIn('isolated-map-secret', response.text)

    def test_selected_school_profile_can_save_its_official_website(self):
        response = self.client.patch('/api/schools/hlby', json={'website': 'https://www.byau.edu.cn/'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.saved[-1]['profile_overrides']['hlby']['website'], 'https://www.byau.edu.cn/')
        self.assertEqual(self.saved[-1]['amap_key'], 'isolated-map-secret')
