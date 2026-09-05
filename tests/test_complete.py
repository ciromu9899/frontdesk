from __future__ import annotations
import json, os, tempfile, threading, urllib.error, urllib.parse, urllib.request, zipfile
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import TestCase, mock
import auth, chat, config as app_config, connectors, disaster_recovery, privacy, providers, rag, sales, state, tools, webchat

class ProviderSelectionTests(TestCase):
    def test_auto_is_local_even_when_cloud_credentials_exist(self):
        environment={"ANTHROPIC_API_KEY":"present","OPENAI_API_KEY":"present"}
        with mock.patch.dict(os.environ,environment,clear=True):
            configuration=app_config.Config(provider="auto").resolve()
        self.assertEqual(configuration.provider,"ollama")

    def test_cloud_provider_can_still_be_selected_explicitly(self):
        configuration=app_config.Config(provider="openai",model="example").resolve()
        self.assertEqual(configuration.provider,"openai")

class DurableStateTests(TestCase):
    def test_sessions_and_delivery_dedup_survive_connections(self):
        with tempfile.TemporaryDirectory() as temporary:
            database=Path(temporary)/"state.db"
            payload={"session_id":"s1","principal":{"subject":"u","roles":["guest"]},"history":[{"role":"user","text":"hello"}]}
            state.save_session("a","web:1",payload,database)
            self.assertEqual(state.load_session("a","web:1",database)["history"][0]["text"],"hello")
            self.assertIsNone(state.load_session("b","web:1",database))
            self.assertFalse(state.already_seen("a","event-1",ttl=600,now=1000,database=database))
            self.assertTrue(state.already_seen("a","event-1",ttl=600,now=1001,database=database))
            self.assertFalse(state.already_seen("b","event-1",ttl=600,now=1001,database=database))

    def test_demo_business_data_is_tenant_scoped(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(os.environ,{"FRONTDESK_STATE_DB":str(Path(temporary)/"state.db")}):
            principal=auth.Principal("operator",("operator",),"tenant-a")
            result=tools.execute(tools.ToolCall("c1","cancel_reservation",{"reservation_id":"R-2001"}),principal=principal,context={"tenant_id":"tenant-a"})
            self.assertFalse(result.is_error,result.content)
            self.assertEqual(tools.load_store("tenant-a")["reservations"]["R-2001"]["status"],"cancelled")
            self.assertEqual(tools.load_store("tenant-b")["reservations"]["R-2001"]["status"],"confirmed")

    def test_backup_is_integral_and_readable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);source=root/"source.db";destination=root/"backup.db"
            state.save_session("a","x",{"session_id":"s","principal":{"subject":"u","roles":["guest"]},"history":[]},source)
            report=disaster_recovery.backup(destination,source)
            self.assertEqual(report["integrity"],"ok");self.assertEqual(report["sessions"],1)
            self.assertEqual(state.load_session("a","x",destination)["session_id"],"s")

class PrivacyWorkflowTests(TestCase):
    def test_export_and_confirmed_delete_are_tenant_scoped_and_atomic(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(os.environ,{"FRONTDESK_STATE_DB":str(Path(temporary)/"state.db")}), mock.patch("privacy.audit.record"):
            payload={"session_id":"s","principal":{"subject":"customer-1","roles":["customer"]},"history":[{"role":"user","text":"hello"}]}
            state.save_session("tenant-a","web:a",payload);state.save_session("tenant-b","web:b",payload)
            self.assertEqual(len(state.export_subject("tenant-a","customer-1")["sessions"]),1)
            with self.assertRaises(ValueError):privacy.complete_delete("tenant-a","customer-1","missing")
            self.assertIsNotNone(state.load_session("tenant-a","web:a"))
            request=privacy.submit("tenant-a","customer-1","delete")
            removed=privacy.complete_delete("tenant-a","customer-1",request["request_id"])
            self.assertEqual(removed,{"sessions":1});self.assertIsNone(state.load_session("tenant-a","web:a"))
            self.assertIsNotNone(state.load_session("tenant-b","web:b"))

class TenantConnectorTests(TestCase):
    def test_nondefault_tenant_cannot_reuse_shared_backend_credentials(self):
        with mock.patch.dict(os.environ,{"FRONTDESK_BACKEND_URL":"https://shared.example","FRONTDESK_BACKEND_TOKEN":"shared"},clear=True):
            with self.assertRaises(connectors.ConnectorError):connectors.live_backend("tenant-a")

    def test_exact_profiles_use_distinct_token_environment_variables(self):
        with tempfile.TemporaryDirectory() as temporary:
            profile=Path(temporary)/"backends.json"
            profile.write_text(json.dumps({"tenants":{"tenant-a":{"base_url":"https://a.example","token_env":"TOKEN_A"},"tenant-b":{"base_url":"https://b.example","token_env":"TOKEN_B"}}}),encoding="utf-8")
            environment={"FRONTDESK_TENANT_BACKENDS_FILE":str(profile),"TOKEN_A":"secret-a","TOKEN_B":"secret-b"}
            with mock.patch.dict(os.environ,environment):
                backend_a=connectors.live_backend("tenant-a");backend_b=connectors.live_backend("tenant-b")
            self.assertEqual((backend_a.config.base_url,backend_a.config.token),("https://a.example","secret-a"))
            self.assertEqual((backend_b.config.base_url,backend_b.config.token),("https://b.example","secret-b"))

class OllamaProviderTests(TestCase):
    def test_healthcare_medication_decision_is_deterministically_handed_off(self):
        configuration=app_config.Config(provider="echo",persona="healthcare",use_tools=True).resolve()
        session=chat.Session(configuration,chat.Style(False),auth.Principal("guest",("guest",),"tenant-a"))
        result=tools.ToolResult("policy-1","request_human_handoff",json.dumps({"handoff_id":"H-SAFE"}))
        with mock.patch.object(session,"_invoke",return_value=result) as invoke:
            reply=session.ask("Should I stop my prescribed medication today?")
        invoke.assert_called_once()
        self.assertTrue(reply.startswith("I cannot tell you"))
        self.assertIn("H-SAFE",reply)
        self.assertEqual([turn.role for turn in session.history],["user","assistant","tool","assistant"])

    def test_provider_url_rejects_nonlocal_plain_http(self):
        with self.assertRaises(providers.ProviderError):
            list(providers._post_lines("http://example.com/api/chat", {}, {}))

    def test_customer_requests_disable_hidden_thinking_and_bound_generation(self):
        captured={}
        def fake_post(url,headers,payload):
            captured.update(payload)
            yield json.dumps({"message":{"content":"ready"},"done":True})
        configuration=app_config.Config(provider="ollama",model="qwen3:8b",max_tokens=2000,show_thinking=False).resolve()
        with mock.patch("providers._post_lines",fake_post):
            chunks=list(providers.OllamaProvider(configuration).stream("system",[providers.Turn("user","hello")],[]))
        self.assertFalse(captured["think"]);self.assertEqual(captured["options"]["num_predict"],2000)
        self.assertEqual([chunk.text for chunk in chunks if chunk.kind=="text"],["ready"])

    def test_tool_history_uses_ollama_object_arguments_and_tool_name(self):
        history=[providers.Turn("assistant",tool_calls=[tools.ToolCall("call-1","lookup",{"id":"A-1"})]),providers.Turn("tool",tool_results=[tools.ToolResult("call-1","lookup","{\"status\":\"ready\"}")])]
        messages=providers._ollama_messages(history,"system")
        self.assertEqual(messages[1]["tool_calls"][0]["function"]["arguments"],{"id":"A-1"})
        self.assertEqual(messages[2]["tool_name"],"lookup")

class DocumentIngestionTests(TestCase):
    def test_turn_grounding_is_tenant_scoped_and_permission_gated(self):
        configuration=app_config.Config(provider="echo",use_tools=True).resolve()
        principal=auth.Principal("guest",("guest",),"tenant-a")
        session=chat.Session(configuration,chat.Style(False),principal)
        hit=rag.SearchHit("policy.md",2,4.2,"Tenant A policy")
        with mock.patch("chat.rag.search",return_value=[hit]) as search:
            context=session._grounding_context("returns")
        search.assert_called_once_with("returns",limit=3,tenant_id="tenant-a")
        self.assertIn("policy.md#chunk-2",context)
        denied=chat.Session(configuration,chat.Style(False),auth.Principal("none",(),"tenant-b"))
        with mock.patch("chat.rag.search") as search:
            self.assertEqual(denied._grounding_context("returns"),"")
        search.assert_not_called()
        with mock.patch("chat.rag.search",side_effect=OSError("unavailable")):
            fallback=session._grounding_context("returns")
        self.assertIn("Do not guess company-specific",fallback)

    def test_office_xml_rejects_entities_and_oversized_entries(self):
        from defusedxml.common import DefusedXmlException
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            malicious=root/"entity.docx"
            with zipfile.ZipFile(malicious,"w") as archive:
                archive.writestr("word/document.xml",'<!DOCTYPE x [<!ENTITY leak "secret">]><w:document xmlns:w="urn:w"><w:t>&leak;</w:t></w:document>')
            with self.assertRaises(DefusedXmlException):
                rag._plain_text(malicious)
            oversized=root/"oversized.docx"
            with zipfile.ZipFile(oversized,"w") as archive:
                archive.writestr("word/document.xml",b"x"*(rag.MAX_OFFICE_XML_ENTRY+1))
            with self.assertRaisesRegex(ValueError,"unsafe Office XML entry"):
                rag._plain_text(oversized)

    def test_docx_is_ingested_and_hybrid_search_finds_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);knowledge=root/"knowledge";knowledge.mkdir();index=root/"index.json"
            with zipfile.ZipFile(knowledge/"policy.docx","w") as archive:
                archive.writestr("word/document.xml",'<w:document xmlns:w="urn:w"><w:body><w:p><w:r><w:t>Manufacturing defects have a one year warranty</w:t></w:r></w:p></w:body></w:document>')
            built=rag.build_index(knowledge,index);self.assertEqual(built["files"],1)
            hits=rag.search("manufacturing warranty",index_path=index)
            self.assertTrue(hits);self.assertEqual(hits[0].source,"policy.docx")

    def test_pdf_pptx_and_xlsx_readers_are_exercised(self):
        from pypdf import PdfWriter
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);knowledge=root/"knowledge";knowledge.mkdir();index=root/"index.json"
            writer=PdfWriter();writer.add_blank_page(width=72,height=72)
            with (knowledge/"blank.pdf").open("wb") as output:writer.write(output)
            self.assertEqual(rag._plain_text(knowledge/"blank.pdf"),"")
            with zipfile.ZipFile(knowledge/"slides.pptx","w") as archive:
                archive.writestr("ppt/slides/slide1.xml",'<p:sld xmlns:p="urn:p" xmlns:a="urn:a"><a:t>Emergency replacement ships overnight</a:t></p:sld>')
            with zipfile.ZipFile(knowledge/"prices.xlsx","w") as archive:
                archive.writestr("xl/sharedStrings.xml",'<sst xmlns="urn:x"><si><t>Premium support costs twenty dollars</t></si></sst>')
            built=rag.build_index(knowledge,index);self.assertEqual(built["files"],2)
            self.assertEqual(rag.search("overnight replacement",index_path=index)[0].source,"slides.pptx")
            self.assertEqual(rag.search("premium support twenty dollars",index_path=index)[0].source,"prices.xlsx")

    def test_tenant_knowledge_indexes_do_not_cross(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(rag,"KNOWLEDGE_DIR",Path(temporary)/"knowledge"), mock.patch.object(rag,"ROOT",Path(temporary)):
            directory_a,index_a=rag.tenant_paths("tenant-a");directory_b,index_b=rag.tenant_paths("tenant-b")
            directory_a.mkdir(parents=True);directory_b.mkdir(parents=True)
            (directory_a/"policy.txt").write_text("Alpha-only glacier replacement policy",encoding="utf-8")
            (directory_b/"policy.txt").write_text("Beta-only desert replacement policy",encoding="utf-8")
            rag.build_index(tenant_id="tenant-a");rag.build_index(tenant_id="tenant-b")
            self.assertIn("glacier",rag.search("replacement",tenant_id="tenant-a")[0].text)
            self.assertNotIn("glacier",rag.search("replacement",tenant_id="tenant-b")[0].text)
            self.assertNotEqual(index_a,index_b)

class WebChatTests(TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.env=mock.patch.dict(os.environ,{"FRONTDESK_STATE_DB":str(Path(self.temporary.name)/"state.db"),"FRONTDESK_WEB_PROVIDER":"echo"});self.env.start();self.addCleanup(self.env.stop)
        self.server=ThreadingHTTPServer(("127.0.0.1",0),webchat.WebChatHandler);self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(lambda:(self.server.shutdown(),self.server.server_close(),self.thread.join(timeout=2)))
        self.opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()));self.base=f"http://127.0.0.1:{self.server.server_port}"

    def test_spanish_privacy_page_identifies_operator_and_linkedin(self):
        page=self.opener.open(self.base+"/privacy?lang=es").read().decode("utf-8")
        self.assertIn('lang="es"',page)
        self.assertIn("organización que implementó Frontdesk",page)
        self.assertIn("inicio de sesión opcional",page)

    def test_accessible_english_and_spanish_pages(self):
        english=self.opener.open(self.base+"/").read().decode();spanish=self.opener.open(self.base+"/?lang=es").read().decode()
        for marker in ('role="log"','aria-live="polite"','for="message"','class="skip"'):self.assertIn(marker,english)
        self.assertIn('href="/linkedin/start">Sign in for private account actions',english)
        self.assertIn('<html lang="es">',spanish);self.assertIn("Ayuda al cliente",spanish)
        self.assertIn('href="/linkedin/start">Iniciar sesión para acciones privadas',spanish)

    def test_linkedin_start_redirects_and_keeps_the_web_session(self):
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, request, fp, code, message, headers, new_url):
                return None
        opener=urllib.request.build_opener(NoRedirect,urllib.request.HTTPCookieProcessor(CookieJar()))
        target="https://www.linkedin.com/oauth/v2/authorization?client_id=test"
        with mock.patch("webchat.linkedin.authorization_url",return_value=target) as authorization:
            with self.assertRaises(urllib.error.HTTPError) as caught:
                opener.open(self.base+"/linkedin/start")
        self.assertEqual(caught.exception.code,303);self.assertEqual(caught.exception.headers["Location"],target)
        self.assertIn("fd_web_session=",caught.exception.headers["Set-Cookie"])
        self.assertEqual(authorization.call_args.args[0],"web")

    def test_verified_linkedin_identity_is_used_by_web_chat(self):
        handler=webchat.WebChatHandler.__new__(webchat.WebChatHandler);handler.headers={}
        record={"subject":"linkedin:buyer@example.com","trust":"authenticated"}
        with mock.patch("webchat.identity.recall",return_value=record) as recall:
            principal=handler._principal("session-1")
        self.assertEqual(principal.subject,"linkedin:buyer@example.com")
        self.assertEqual(principal.roles,("support",))
        recall.assert_called_once_with("web","session-1",tenant_id="web:default")

    def test_chat_post_and_gpc(self):
        request=urllib.request.Request(self.base+"/",headers={"Sec-GPC":"1"});response=self.opener.open(request);page=response.read().decode();self.assertEqual(response.headers["X-Frontdesk-GPC"],"honored")
        csrf=page.split("const csrf=",1)[1].split(",lang=",1)[0].strip('"')
        post=urllib.request.Request(self.base+"/api/chat",data=json.dumps({"message":"hello","lang":"en"}).encode(),headers={"Content-Type":"application/json","X-CSRF":csrf},method="POST")
        payload=json.loads(self.opener.open(post).read());self.assertIn("hello",payload["reply"])

class ShellieSalesTests(TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.database=Path(self.temporary.name)/"sales.db"
        self.secret="s"*48
        self.order={"payer":{"email_address":"Buyer@Example.com","address":{"country_code":"US"}},"purchase_units":[{"amount":{"currency_code":"USD","value":"2999.00","breakdown":{"tax_total":{"currency_code":"USD","value":"0.00"}}}}]}
        self.event={"id":"WH-1","event_type":"PAYMENT.CAPTURE.COMPLETED","resource":{"id":"CAP-1","status":"COMPLETED","amount":{"value":"2999.00","currency_code":"USD"},"payee":{"merchant_id":"MERCHANT-1"},"supplementary_data":{"related_ids":{"order_id":"ORDER-1"}}}}

    def test_completed_capture_is_idempotent_and_activates_only_when_preapproved(self):
        with mock.patch.dict(os.environ,{"SHELLIE_TAX_MODE":"preapproved","SHELLIE_PAYPAL_MERCHANT_ID":"MERCHANT-1"},clear=False):
            first=sales.process_webhook(self.event,database=self.database,order_loader=lambda _id:self.order)
            second=sales.process_webhook(self.event,database=self.database,order_loader=lambda _id:self.order)
        self.assertEqual(first["entitlement"],"active");self.assertTrue(second["duplicate"])
        self.assertEqual(sales.status(self.database)["orders"],1)

    def test_default_tax_mode_holds_then_authorised_review_releases(self):
        with mock.patch.dict(os.environ,{"SHELLIE_TAX_MODE":"manual"},clear=False):
            result=sales.process_webhook(self.event,database=self.database,order_loader=lambda _id:self.order)
        self.assertEqual(result["entitlement"],"held_tax")
        with self.assertRaises(sales.SalesClaimError):sales.claim("ORDER-1","buyer@example.com",secret=self.secret,database=self.database)
        sales.approve_tax("ORDER-1",actor="tax-owner",jurisdiction="US-CA",note="reviewed",database=self.database)
        claim=sales.claim("ORDER-1","buyer@example.com",secret=self.secret,database=self.database)
        self.assertTrue(claim["license_key"].startswith("FD1-"))
        verified=sales.verify_download_token(claim["download_token"],secret=self.secret,database=self.database)
        self.assertEqual(verified["order_id"],"ORDER-1")

    def test_wrong_buyer_and_modified_download_token_are_rejected(self):
        with mock.patch.dict(os.environ,{"SHELLIE_TAX_MODE":"preapproved"},clear=False):
            sales.process_webhook(self.event,database=self.database,order_loader=lambda _id:self.order)
        with self.assertRaises(sales.SalesClaimError):sales.claim("ORDER-1","other@example.com",secret=self.secret,database=self.database)
        token=sales.claim("ORDER-1","BUYER@example.com",secret=self.secret,database=self.database)["download_token"]
        with self.assertRaises(sales.SalesClaimError):sales.verify_download_token(token[:-1]+("A" if token[-1]!="A" else "B"),secret=self.secret,database=self.database)

    def test_wrong_price_or_merchant_never_creates_an_order(self):
        wrong=json.loads(json.dumps(self.event));wrong["resource"]["amount"]["value"]="29.99"
        with self.assertRaises(sales.SalesVerificationError):sales.process_webhook(wrong,database=self.database,order_loader=lambda _id:self.order)
        self.assertEqual(sales.status(self.database)["orders"],0)
        other=json.loads(json.dumps(self.event));other["id"]="WH-2"
        with mock.patch.dict(os.environ,{"SHELLIE_PAYPAL_MERCHANT_ID":"DIFFERENT"},clear=False), self.assertRaises(sales.SalesVerificationError):
            sales.process_webhook(other,database=self.database,order_loader=lambda _id:self.order)

    def test_refund_and_dispute_suspend_or_revoke_entitlement(self):
        with mock.patch.dict(os.environ,{"SHELLIE_TAX_MODE":"preapproved"},clear=False):
            sales.process_webhook(self.event,database=self.database,order_loader=lambda _id:self.order)
        dispute={"id":"WH-2","event_type":"CUSTOMER.DISPUTE.CREATED","resource":{"id":"PP-D-1","disputed_transactions":[{"transaction_info":{"seller_transaction_id":"CAP-1"}}]}}
        self.assertEqual(sales.process_webhook(dispute,database=self.database)["entitlement"],"suspended")
        refunded={"id":"WH-3","event_type":"PAYMENT.CAPTURE.REFUNDED","resource":{"id":"REF-1","supplementary_data":{"related_ids":{"capture_id":"CAP-1"}}}}
        self.assertEqual(sales.process_webhook(refunded,database=self.database)["entitlement"],"revoked")

    def test_webhook_verification_uses_shellie_specific_configuration(self):
        headers={"PAYPAL-AUTH-ALGO":"SHA256withRSA","PAYPAL-CERT-URL":"https://api.paypal.com/cert","PAYPAL-TRANSMISSION-ID":"T-1","PAYPAL-TRANSMISSION-SIG":"sig","PAYPAL-TRANSMISSION-TIME":"2026-08-22T00:00:00Z"}
        with mock.patch.dict(os.environ,{"SHELLIE_PAYPAL_WEBHOOK_ID":"WH-ID"},clear=False), mock.patch.object(sales,"_authed",return_value={"verification_status":"SUCCESS"}) as request:
            self.assertTrue(sales.verify_webhook(headers,self.event))
        self.assertEqual(request.call_args.args[1],"/v1/notifications/verify-webhook-signature")

    def test_refund_uses_the_shellie_paypal_api_and_an_idempotency_key(self):
        with mock.patch.object(sales,"_authed",return_value={"id":"REF-1","status":"COMPLETED"}) as request:
            result=sales.refund("CAP-1",reason="Approved refund")
        self.assertEqual(result["status"],"COMPLETED")
        self.assertEqual(request.call_args.args[1],"/v2/payments/captures/CAP-1/refund")
        self.assertTrue(request.call_args.args[3].startswith("shellie-refund-"))
