"""高德适配器契约测试：HTTP 使用隔离 transport，测试资料不会入库。"""
import importlib
import json
from pathlib import Path
import sqlite3
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch, Mock
import httpx
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
import db
from agent import Agent

KEY='isolated-amap-key'
CFG={'amap_key':KEY,'amap_city':'大庆市','amap_campus_name':'黑龙江八一农垦大学','amap_campus_location':'125.100000,46.100000','amap_radius':3000}
def poi(**kw):
    return {'id':'BTEST001','name':'测试餐馆','location':'125.110000,46.110000','address':'测试路1号','cityname':'大庆市','parent':'','business':{'rating':'4.6','cost':'30','tel':'123','opentime_today':'09:00-21:00'},**kw}

class MapsTests(unittest.TestCase):
    def setUp(self):
        self.m=importlib.import_module('maps')
        self.requests=[]
    def client(self,pois=None,payload=None,status=200):
        def respond(req):
            self.requests.append(req)
            return httpx.Response(status,json=payload if payload is not None else {'status':'1','pois':pois if pois is not None else [poi()]})
        return self.m.AmapClient(CFG,httpx.MockTransport(respond))
    def test_keyword_search_requests_business_fields_and_strict_city(self):
        result=self.client().search('餐馆',scope='city')
        request=self.requests[0]
        self.assertEqual(request.url.host,'restapi.amap.com')
        self.assertEqual(request.url.path,'/v5/place/text')
        self.assertEqual(request.url.params['region'],'大庆市')
        self.assertEqual(request.url.params['city_limit'],'true')
        self.assertIn('business',request.url.params['show_fields'])
        self.assertEqual(result['results'][0]['rating'],'4.6')
        self.assertEqual(result['results'][0]['cost'],'30')
        self.assertNotIn(KEY,json.dumps(result))
    def test_nearby_search_uses_school_origin_not_user_location(self):
        result=self.client().search('咖啡',scope='nearby')
        self.assertEqual(self.requests[0].url.path,'/v5/place/around')
        self.assertEqual(self.requests[0].url.params['location'],CFG['amap_campus_location'])
        row=result['results'][0]
        self.assertIn('from=',row['navigation_url'])
        self.assertIn('校园边界',result['notice'])
    def test_missing_rating_is_not_zero_or_invented_review(self):
        result=self.client([poi(business={'rating':[],'cost':[],'tel':[]})]).search('图书馆',scope='city')
        row=result['results'][0]
        self.assertIsNone(row['rating']);self.assertIsNone(row['cost'])
        self.assertEqual(row['review_status'],'not_provided')
        self.assertNotIn('reviews',row)
    def test_no_key_makes_no_request_and_explains_configuration(self):
        with patch('maps.httpx.Client') as network:
            result=self.m.AmapClient({}).search('餐馆')
            network.assert_not_called()
        self.assertFalse(result['available']);self.assertTrue(result['config_needed'])
        self.assertIn('高德',result['message'])
    def test_api_error_and_transport_error_do_not_expose_key(self):
        for code in ['10001','10002','10003','10009','10044']:
            result=self.client(payload={'status':'0','infocode':code,'info':KEY}).search('餐馆',scope='city')
            self.assertFalse(result['available']);self.assertIn(code,result['message'])
            self.assertNotIn(KEY,json.dumps(result))
        def fail(req):raise httpx.ConnectError('secret='+KEY,request=req)
        result=self.m.AmapClient(CFG,httpx.MockTransport(fail)).search('餐馆',scope='city')
        self.assertNotIn(KEY,json.dumps(result));self.assertFalse(result['available'])

    def test_http_client_info_logs_redact_the_query_key(self):
        with self.assertLogs('httpx',level='INFO') as logs:
            self.client().search('餐馆',scope='city')
        self.assertNotIn(KEY,'\n'.join(logs.output))
    def test_rating_sort_is_only_for_current_candidates_and_keeps_missing_last(self):
        rows=[poi(id='A',business={'rating':[]}),poi(id='B',business={'rating':'4.2'}),poi(id='C',business={'rating':'4.9'})]
        result=self.client(rows).search('餐馆',scope='nearby',sort='rating',limit=3)
        self.assertEqual([r['id'] for r in result['results']],['C','B','A'])
        self.assertIn('本次',result['notice'])
    def test_invalid_coordinate_and_inputs_are_rejected_before_http(self):
        for coordinate in ['999,1','nan,1','1,inf','123','a,b']:
            with self.assertRaises(ValueError):self.m.validate_location(coordinate)
        for kwargs in [{'query':''},{'query':'a'*81},{'query':'餐馆','scope':'unknown'},{'query':'餐馆','radius':50001}]:
            with self.assertRaises(ValueError):self.client().search(**kwargs)
        self.assertEqual(self.requests,[])
    def test_unresolved_or_ambiguous_school_does_not_guess_origin(self):
        cfg={**CFG,'amap_campus_location':''}
        def respond(req):
            return httpx.Response(200,json={'status':'1','pois':[poi(name=CFG['amap_campus_name']),poi(id='OTHER',name=CFG['amap_campus_name'],location='126,46')]})
        result=self.m.AmapClient(cfg,httpx.MockTransport(respond)).search('咖啡',scope='nearby')
        self.assertFalse(result['available']);self.assertIn('坐标',result['message'])
    def test_school_origin_is_resolved_from_exact_name(self):
        cfg={**CFG,'amap_campus_location':''}
        def respond(req):
            self.requests.append(req)
            return httpx.Response(200,json={'status':'1','pois':[poi(id='SCHOOL',name=CFG['amap_campus_name'])]})
        result=self.m.AmapClient(cfg,httpx.MockTransport(respond)).search('咖啡',scope='nearby')
        self.assertEqual(len(self.requests),2)
        self.assertEqual(self.requests[1].url.params['location'],'125.110000,46.110000')
        self.assertTrue(result['available'])
    def test_campus_results_need_school_identity_not_just_nearby_coordinates(self):
        rows=[poi(),poi(id='LIBRARY',name='黑龙江八一农垦大学图书馆')]
        result=self.client(rows).search('图书馆',scope='campus')
        self.assertEqual([r['id'] for r in result['results']],['LIBRARY'])
    def test_detail_uses_poi_id_and_returns_source_links_without_key(self):
        result=self.client().detail('BTEST001')
        self.assertEqual(self.requests[0].url.path,'/v5/place/detail')
        self.assertEqual(self.requests[0].url.params['id'],'BTEST001')
        self.assertTrue(result['results'][0]['source_url'].startswith('https://uri.amap.com/poidetail?'))
        self.assertNotIn(KEY,json.dumps(result))
        with self.assertRaises(ValueError):self.client().detail('bad|injected')
    def test_public_configuration_masks_key_and_patch_preserves_other_provider(self):
        cfg={**CFG,'api_key':'existing-deepseek-key'}
        public=self.m.public_config(cfg)
        self.assertTrue(public['has_key']);self.assertNotIn(KEY,json.dumps(public))
        updated=self.m.update_config(cfg,{'city':'大庆市','campus_name':CFG['amap_campus_name'],'campus_location':'','radius':4000})
        self.assertEqual(updated['amap_key'],KEY);self.assertEqual(updated['api_key'],'existing-deepseek-key')
        self.assertEqual(self.m.update_config(cfg,{'api_key':''})['amap_key'],'')
        with self.assertRaises(ValueError):self.m.update_config(cfg,{'campus_location':'999,0'})
    def test_explicit_place_intents_do_not_capture_generic_food_or_services(self):
        for query in ['学校附近评分高的餐馆','校外吃什么','校内图书馆在哪','用高德找咖啡店','校外这家餐馆评价怎么样']:
            self.assertIsNotNone(self.m.parse_place_request(query),query)
        for query in ['中午吃什么','奖学金怎么申请','找教务处电话','推荐一本书']:
            self.assertIsNone(self.m.parse_place_request(query),query)
        for query in ['校内奖学金怎么申请','校内图书馆借阅规定','学校附近医院医保报销流程']:
            self.assertIsNone(self.m.parse_place_request(query),query)
        args=self.m.parse_place_request('学校附近3公里内的咖啡店')
        self.assertEqual(args['query'],'咖啡店');self.assertEqual(args['radius'],3000)
        self.assertEqual(self.m.parse_place_request('一食堂在哪')['scope'],'campus')
    def test_campus_search_keeps_local_evidence_when_no_key(self):
        rag=Mock();rag.search.return_value=[{'doc_id':42,'title':'图书馆介绍','source':'用户提供','content':'图书馆位于校内。'}]
        result=self.m.search_places('图书馆',scope='campus',cfg={},rag=rag)
        self.assertEqual(result['local_references'][0]['doc_id'],42)
        self.assertFalse(result['available'])
    def test_agent_routes_places_before_random_food_and_stores_card_without_secret(self):
        conn=sqlite3.connect(':memory:');conn.row_factory=sqlite3.Row
        try:
            with patch('db.get_conn',return_value=conn),patch('agent.get_conn',return_value=conn),patch('agent.LLMClient') as llm:
                db.init_db()
                events=list(Agent(None,None,None,lambda:{}).chat(None,'校外吃什么'))
                answer=next(e['data'] for e in events if e['type']=='answer')
                self.assertEqual(answer['tool_events'][0]['name'],'search_campus_places')
                self.assertIn('配置',answer['content']);llm.assert_not_called()
                self.assertNotIn(KEY,json.dumps(answer))
        finally:conn.close()

    def test_detail_button_context_calls_detail_tool_without_putting_poi_id_in_user_text(self):
        conn=sqlite3.connect(':memory:');conn.row_factory=sqlite3.Row
        try:
            with patch('db.get_conn',return_value=conn),patch('agent.get_conn',return_value=conn),patch('agent.LLMClient') as llm:
                db.init_db()
                events=list(Agent(None,None,None,lambda:{}).chat(None,'查看餐馆的地点详情',place_id='BTEST001'))
                answer=next(e['data'] for e in events if e['type']=='answer')
                self.assertEqual(answer['tool_events'][0]['name'],'get_campus_place_detail')
                self.assertEqual(conn.execute("SELECT content FROM messages WHERE role='user'").fetchone()[0],'查看餐馆的地点详情')
                llm.assert_not_called()
        finally:conn.close()

    def test_removed_amap_settings_command_does_not_open_a_configuration_card(self):
        conn=sqlite3.connect(':memory:');conn.row_factory=sqlite3.Row
        try:
            with patch('db.get_conn',return_value=conn),patch('agent.get_conn',return_value=conn):
                db.init_db()
                events=list(Agent(None,None,None,lambda:{}).chat(None,'配置高德'))
                answer=next(e['data'] for e in events if e['type']=='answer')
                self.assertEqual(answer['tool_events'],[])
        finally:conn.close()

    def test_followup_model_can_see_saved_place_facts_without_changing_visible_messages(self):
        conn=sqlite3.connect(':memory:');conn.row_factory=sqlite3.Row
        captured=[]
        class LLM:
            def chat_stream(self,messages,tools):
                captured.extend(messages)
                yield {'type':'final','message':SimpleNamespace(content='请核对地图中的地址。',tool_calls=None)}
        try:
            with patch('db.get_conn',return_value=conn),patch('agent.get_conn',return_value=conn):
                db.init_db()
                agent=Agent(None,None,None,lambda:{**CFG,'api_key':'isolated-model-key'})
                with patch('maps.AmapClient._request',return_value=[poi()]):
                    first=list(agent.chat(None,'学校附近的餐馆'))
                cid=next(e['data'] for e in first if e['type']=='conversation')
                with patch('agent.LLMClient',return_value=LLM()):
                    list(agent.chat(cid,'第一家地址是什么'))
                assistant=next(m['content'] for m in captured if m['role']=='assistant')
                self.assertIn('BTEST001',assistant);self.assertIn('4.6',assistant);self.assertIn('测试路1号',assistant)
                visible=conn.execute("SELECT content FROM messages WHERE conversation_id=? AND role='assistant' ORDER BY id LIMIT 1",(cid,)).fetchone()[0]
                self.assertNotIn('BTEST001',visible)
        finally:conn.close()

if __name__=='__main__':unittest.main()
