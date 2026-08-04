from __future__ import annotations
from contextlib import closing
import json, os, sqlite3, tempfile, unittest, urllib.error, urllib.request
from pathlib import Path
from tests.test_release import running_server

class Security211Tests(unittest.TestCase):
    def test_https_public_origin_enables_secure_cookie(self):
        from auth_session import build_cookie, build_clear_cookie

        previous = os.environ.get('STUDIO_PUBLIC_ORIGIN')
        os.environ['STUDIO_PUBLIC_ORIGIN'] = 'https://studio.example.com'
        try:
            self.assertIn('; Secure', build_cookie('token'))
            self.assertIn('; Secure', build_clear_cookie())
        finally:
            if previous is None:
                os.environ.pop('STUDIO_PUBLIC_ORIGIN', None)
            else:
                os.environ['STUDIO_PUBLIC_ORIGIN'] = previous

    def test_dns_rebinding_is_blocked(self):
        with running_server() as (client, db, base):
            req=urllib.request.Request(base+'/api/v2/workflows',data=b'{"sourceInput":"x"}',method='POST',headers={'Content-Type':'application/json','Host':'evil.example','Origin':'http://evil.example'})
            # 使用无代理 opener 避免系统代理拦截 Host 头异常的请求
            no_proxy_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with self.assertRaises(urllib.error.HTTPError) as ctx: no_proxy_opener.open(req,timeout=5)
            self.assertEqual(ctx.exception.code,403)
            body=json.loads(ctx.exception.read())
            self.assertEqual(body['error']['code'],'host_forbidden')

    def test_base_url_change_requires_key_reentry(self):
        with running_server() as (client, db, base):
            status,_,_=client.request('/api/v2/settings','PATCH',{'ai':{'providerId':'openai-compatible','baseUrl':'https://api.openai.com/v1','apiKey':'sk-old','model':'gpt-test'}})
            self.assertEqual(status,200)
            status,body,_=client.request('/api/v2/settings','PATCH',{'ai':{'providerId':'openai-compatible','baseUrl':'https://example.com/v1','apiKey':'','model':'gpt-test'}})
            self.assertEqual(status,400)
            self.assertEqual(body['error']['code'],'ai_key_reentry_required')
            with closing(sqlite3.connect(db)) as conn: raw=conn.execute("select value_json from settings where key='ai'").fetchone()[0]
            self.assertNotIn('sk-old',raw); self.assertIn('enc:v1:',raw)

    def test_legacy_database_migrates(self):
        temp=tempfile.TemporaryDirectory(); path=Path(temp.name)/'legacy.db'
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("create table projects(id text primary key,title text not null)")
            conn.execute("insert into projects(id,title) values('old','旧文章')")
            conn.execute("create table settings(key text primary key,value_json text not null,updated_at text not null)")
            conn.execute("insert into settings values('ai',?, 'old')",(json.dumps({'apiKey':'plaintext','baseUrl':'https://api.openai.com/v1'}),))
            conn.commit()
        old=os.environ.get('STUDIO_DB'); old_key=os.environ.get('STUDIO_MASTER_KEY_FILE'); os.environ['STUDIO_DB']=str(path); os.environ['STUDIO_MASTER_KEY_FILE']=str(Path(temp.name)/'master.key')
        try:
            from db import init_db, list_projects
            init_db(); rows=list_projects(); self.assertEqual(rows[0]['id'],'old')
            with closing(sqlite3.connect(path)) as conn:
                version=conn.execute('select version from schema_meta').fetchone()[0]
                raw=conn.execute("select value_json from settings where key='ai'").fetchone()[0]
            self.assertEqual(version,213); self.assertNotIn('plaintext',raw); self.assertIn('enc:v1:',raw); self.assertTrue(list(path.parent.glob(path.name+'.pre-2.1.2-v0-*.bak')))
        finally:
            if old is None: os.environ.pop('STUDIO_DB',None)
            else: os.environ['STUDIO_DB']=old
            if old_key is None: os.environ.pop('STUDIO_MASTER_KEY_FILE',None)
            else: os.environ['STUDIO_MASTER_KEY_FILE']=old_key
            temp.cleanup()

    def test_retry_stays_on_same_project(self):
        with running_server() as (client,db,base):
            status,result,_=client.request('/api/v2/workflows','POST',{'sourceInput':'测试原文章重试'})
            task=client.wait_task(result['task']['id'])
            with closing(sqlite3.connect(db)) as conn:
                conn.execute("update tasks set status='failed',error_code='forced' where id=?",(task['id'],))
                conn.commit()
            status,retry,_=client.request(f"/api/v2/tasks/{task['id']}/retry",'POST',{'retryMode':'review_only'})
            self.assertEqual(status,202); self.assertEqual(retry['project']['id'],result['project']['id'])
            self.assertNotEqual(retry['task']['id'],task['id'])

    def test_body_edit_invalidates_ai_review(self):
        with running_server() as (client,db,base):
            status,result,_=client.request('/api/v2/workflows','POST',{'sourceInput':'测试审校失效','autoReview':True})
            client.wait_task(result['task']['id'])
            _,project,_=client.request(f"/api/v2/projects/{result['project']['id']}")
            self.assertTrue(project['review'])
            status,updated,_=client.request(f"/api/v2/projects/{project['id']}",'PATCH',{'bodyMarkdown':project['bodyMarkdown']+'\n新内容'},{'If-Match':str(project['revision'])})
            self.assertEqual(status,200); self.assertEqual(updated['review'],[]); self.assertFalse(updated['reviewApproved']); self.assertEqual(updated['reviewFingerprint'],'')

    def test_frontend_autosave_is_project_scoped(self):
        js=(Path(__file__).resolve().parents[1]/'web'/'app.js').read_text(encoding='utf-8')
        self.assertIn('saveTimers: new Map()',js)
        self.assertIn('pendingSaves: new Map()',js); self.assertIn('saveChains: new Map()',js)
        self.assertIn('const boundProjectId=state.currentProject?.id',js)
        self.assertIn('scheduleProjectSave(boundProjectId,field,element.value)',js)
        self.assertIn('!state.dirtyProjects.has(projectId)',js)
        self.assertNotIn('state.saveTimer = setTimeout',js)

if __name__=='__main__': unittest.main()
