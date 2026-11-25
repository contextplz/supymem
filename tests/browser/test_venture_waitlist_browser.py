"""Local rendered-form acceptance; routed ASGI responses are not production-network evidence."""
from urllib.parse import urlsplit
import pytest
from playwright.sync_api import sync_playwright
from tests.security.test_connector_oauth_boundary import client as client
from tests.security.test_venture_waitlist import setup_waitlist
from tests.security.test_durable_journey_boundary import request


@pytest.mark.parametrize('viewport',[{'width':1440,'height':900},{'width':390,'height':844}])
def test_native_consent_submission_and_mobile_layout(client, viewport, tmp_path):
    vid,_,path,_=setup_waitlist(client)
    from app.conversion import waitlist
    posts=[]
    with sync_playwright() as p:
        browser=p.firefox.launch(headless=True)
        page=browser.new_page(viewport=viewport)
        def route_request(route):
            incoming=route.request
            target=urlsplit(incoming.url)
            assert target.hostname=='gateway.supymem.com'
            if incoming.method=='POST':posts.append(incoming.post_data)
            result=client.request(incoming.method,target.path,content=incoming.post_data_buffer,
                headers={k:v for k,v in incoming.headers.items() if k in {'content-type','accept'}})
            route.fulfill(status=result.status_code,headers=dict(result.headers),body=result.content)
        page.route('**/*',route_request)
        page.goto(waitlist.public_url(vid))
        assert page.get_by_role('heading',level=1).inner_text().startswith('Join ')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.get_by_role('textbox',name='Email address').fill('consenting@local-business.example')
        page.get_by_role('button',name='Request early access').click()
        assert posts==[]
        page.get_by_role('checkbox').check()
        page.screenshot(path=str(tmp_path/'consented-form.png'),full_page=True)
        page.get_by_role('button',name='Request early access').click()
        page.get_by_role('heading',name='Request recorded').wait_for()
        assert len(posts)==1
        assert request(client,'GET',path).json()['total']==1
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        browser.close()
