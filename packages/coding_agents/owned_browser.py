"""Private background Chrome context owned by a claimed Supymem executor.

No attachment to the founder's browser/profile, exposed CDP port or raw model
script. Native authority/admission hooks are mandatory. General-browser mode
may type reviewed non-secret fill values and pause for founder handoff. Form
secrets and service workers stay withheld; tabs, frames and CSV downloads are
confined to exact controller-issued fixture grants.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qsl, urljoin, urlsplit
from uuid import uuid4

from coding_agents.browser_pause import (
    classify_control,
    field_kind,
    founder_prompt,
    is_safe_type_target,
    page_requires_pause,
)
from coding_agents.jev_decision import Control, Observation, Candidate, verify_current
from coding_agents.computer_use_gate import EffectUncertain
from coding_agents.cu_page_context import bounded_page_observation, bounded_page_text
from core.redact import redact_string

MAX_PASSIVE_BODY_BYTES = 16 * 1024 * 1024


# Collected in the same existing document evaluation as the load witness.
# Whole strings are either retained or explicitly omitted; field values never
# enter ordinary worker receipts. These are inputs, not acceptance evidence.
_CUAI_SOURCE_JS = r'''(reviewed=[]) => {
  const out={facts:[],fields:[],omitted:{facts:0,fields:0,values:0}};
  const safe=e=>e && !e.closest('script,style,noscript,template,[hidden],[aria-hidden="true"]')
    && e.checkVisibility?.({checkOpacity:true,checkVisibilityCSS:true});
  const textOf=e=>{
    let text='',scanned=0,omitted=false;
    const walk=(n,depth=0)=>{
      if(++scanned>256||depth>32||text.length>700){omitted=true;return;}
      if(n.nodeType===Node.TEXT_NODE){text+=n.textContent||'';return;}
      if(n.nodeType!==Node.ELEMENT_NODE||/^(INPUT|SELECT|TEXTAREA|BUTTON)$/.test(n.tagName)||!safe(n))return;
      for(const child of n.childNodes){walk(child,depth+1);if(omitted)break;}
    };walk(e);return omitted||text.length>700?null:text.trim().replace(/\s+/g,' ');
  };
  const add=e=>{
    if(out.facts.length>=32){out.omitted.facts++;return;}
    if(!safe(e))return;
    const text=textOf(e);
    if(text===null){out.omitted.facts++;return;}
    if(!text)return;
    if(text.length>700){out.omitted.facts++;return;}
    if(!out.facts.includes(text))out.facts.push(text);
  };
  for(const e of [...document.querySelectorAll('h1,h2,h3,p,dl,table tr,li')].slice(0,1000))add(e);
  if(document.querySelectorAll('h1,h2,h3,p,dl,table tr,li').length>1000)out.omitted.facts++;
  const fields=document.querySelectorAll('input,select,textarea');
  if(fields.length>128)out.omitted.fields+=fields.length-128;
  for(const e of Array.from(fields).slice(0,128)){
    const type=(e.type||'').toLowerCase();
    if(!safe(e)||['hidden','password','file','button','submit','reset','image'].includes(type))continue;
    if(out.fields.length>=32){out.omitted.fields++;continue;}
    let label=e.labels?.[0]?textOf(e.labels[0]):'';
    if(label===null){out.omitted.fields++;continue;}
    label=(label||e.getAttribute('aria-label')||e.name||e.placeholder||'field').trim().replace(/\s+/g,' ');
    if(label.length>160){out.omitted.fields++;continue;}
    // Classification is conservative across every source-associated label;
    // these hints are never exported as a field label or value.
    const associated=[...(e.labels||[])].map(n=>n.textContent||'').join(' ');
    const referenced=(e.getAttribute('aria-labelledby')||'').split(/\s+/)
      .map(id=>document.getElementById(id)?.textContent||'').join(' ');
    const hints=[label,associated,referenced,e.getAttribute('aria-label')||'',e.placeholder||'',e.name||'',e.autocomplete||''].join(' ');
    if(/password|passwd|passcode|secret|token|api[\s_-]*key|credential|otp|totp|one[-\s\u2010-\u2015]?time|verification\s*code|security\s*code|sms\s*code|email\s*code|authenticator|\b(?:2fa|mfa)\b|card|cvv|cvc|cc-|billing|payment/i.test(hints)){
      out.omitted.fields++;continue;
    }
    const messages=[];
    const message=text=>{text=(text||'').trim().replace(/\s+/g,' ').slice(0,240);if(text&&!messages.includes(text))messages.push(text);};
    message(e.validationMessage);
    for(const id of ((e.getAttribute('aria-errormessage')||'')+' '+(e.getAttribute('aria-describedby')||'')).split(/\s+/).slice(0,8)){
      const node=e.getRootNode().getElementById?.(id);if(safe(node))message(textOf(node));
    }
    for(const node of [e.nextElementSibling,e.parentElement].filter(Boolean)){
      if(node.matches('[role=alert],.error,.invalid-feedback,[data-error]')&&safe(node))message(textOf(node));
      for(const error of [...node.querySelectorAll('[role=alert],.error,.invalid-feedback,[data-error]')].slice(0,4))if(safe(error))message(textOf(error));
    }
    if(messages.length){
      out.facts.unshift('FIELD VALIDATION (untrusted): '+label+': '+messages.join(' | ').slice(0,480));
      if(out.facts.length>32){out.facts.pop();out.omitted.facts++;}
    }
    const value=type==='checkbox'||type==='radio'?(e.checked?'checked':'unchecked'):
      e.tagName==='SELECT'?(e.multiple?null:(e.selectedOptions[0]?.text||'')):(e.value||'');
    const populated=type==='checkbox'||type==='radio'?!!e.checked:
      e.tagName==='SELECT'&&e.multiple?e.selectedOptions.length>0:!!value;
    const index=value===null?-1:reviewed.indexOf(value);
    if(populated&&index<0)out.omitted.values++;
    // Unknown values stay inside the DOM. Equality exposes only a source-owned
    // index into the already reviewed non-secret values, never the raw value.
    out.fields.push({label,type:e.tagName==='SELECT'?'select':type||'textarea',
      populated,reviewed_value_index:index<0?null:index});
  }
  return out;
}'''

# Fixed document inspection: query is data, never a selector or executable text.
_CUAI_EXTRACT_JS = r'''(query) => {
  const passages=[], seen=new Set(); let omitted=0, scanned=0;
  const safe=e=>e && !e.closest('form,script,style,noscript,template,input,textarea,select,[hidden],[aria-hidden="true"]')
    && e.checkVisibility?.({checkOpacity:true,checkVisibilityCSS:true});
  const textOf=e=>{let text=''; const walk=n=>{
    if(n.nodeType===Node.TEXT_NODE){text+=n.textContent||'';return;}
    if(n.nodeType!==Node.ELEMENT_NODE||/^(INPUT|SELECT|TEXTAREA|BUTTON)$/.test(n.tagName)||!safe(n))return;
    for(const child of n.childNodes)walk(child);
  };walk(e);return text.trim().replace(/\s+/g,' ');};
  const nodes=document.querySelectorAll(query.toLowerCase()==='tables'?'table tr':'h1,h2,h3,h4,p,dl,table tr,li');
  for(const e of nodes){
    if(++scanned>1000){omitted++;break;}
    if(!safe(e))continue;
    const text=textOf(e), at=text.toLowerCase().indexOf(query.toLowerCase());
    if(!text||(query.toLowerCase()!=='tables'&&at<0)||seen.has(text))continue;
    seen.add(text);
    if(passages.length>=6){omitted++;continue;}
    if(text.length>700){
      const start=query.toLowerCase()==='tables'?0:Math.max(0,at-160);
      passages.push(text.slice(start,start+700)); omitted++;
    }else passages.push(text);
  }
  return {passages,omitted};
}'''


def _cuai_redact(value):
    return re.sub(r'(?<!\d)(?:\d[ -]?){13,19}(?!\d)', '[REDACTED]', redact_string(value))


def _cuai_source(value, limit=1800):
    """Redact and bound whole observations, with explicit omission counts."""
    if (not isinstance(value, dict) or set(value) != {'facts','fields','omitted'}
            or not isinstance(value['facts'], list) or not isinstance(value['fields'], list)
            or not isinstance(value['omitted'], dict) or set(value['omitted']) != {'facts','fields','values'}
            or any(type(n) is not int or n < 0 for n in value['omitted'].values())):
        return {'facts':[], 'fields':[], 'omitted':{'facts':1,'fields':1,'values':1}}
    facts, fields, omitted = [], [], dict(value['omitted'])
    for fact in value['facts']:
        if not isinstance(fact,str) or len(fact)>700:
            omitted['facts'] += 1
        else:
            facts.append(_cuai_redact(fact))
    for field in value['fields']:
        if (not isinstance(field,dict) or set(field) != {'label','type','populated','reviewed_value_index'}
                or not isinstance(field['label'],str) or len(field['label'])>160
                or not isinstance(field['type'],str) or len(field['type'])>32
                or type(field['populated']) is not bool
                or field['reviewed_value_index'] is not None and (
                    type(field['reviewed_value_index']) is not int or not 0<=field['reviewed_value_index']<8)):
            omitted['fields'] += 1
        else:
            fields.append({k:_cuai_redact(v) if isinstance(v,str) else v for k,v in field.items()})
    result = {'facts':facts, 'fields':fields, 'omitted':omitted}
    while len(json.dumps(result,ensure_ascii=False,separators=(',',':')).encode())>limit:
        if facts:
            facts.pop()
            omitted['facts'] += 1
        elif fields:
            fields.pop()
            omitted['fields'] += 1
        else:
            break
    return result


def cuai_document_target(load, action):
    """Bind the exact bounded read/scroll parameters to one source document."""
    if not isinstance(load, dict) or action.op not in {"extract", "scroll", "new_tab", "navigate"}:
        raise PermissionError("browser_candidate_invalid")
    body = {key: load.get(key) for key in (
        "session_id", "page_id", "document_id", "sequence", "location", "observation_digest")}
    body["action"] = ({"op": action.op, "text": action.text} if action.op in {"extract", "new_tab", "navigate"}
                      else {"op": action.op, "direction": action.direction, "amount": action.amount})
    return "document_" + hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def strip_check_marker(url: str) -> str:
    """Remove only the reserved analytics key, retaining all other URL bytes."""
    if not isinstance(url, str) or not url:
        return url
    before_fragment, fragment_separator, fragment = url.partition('#')
    prefix, query_separator, query = before_fragment.partition('?')
    if not query_separator:
        return url
    remaining, removed = [], False
    for segment in query.split('&'):
        decoded = parse_qsl(segment, keep_blank_values=True)
        if len(decoded) == 1 and decoded[0][0] == 'supymem_check':
            removed = True
        else:
            remaining.append(segment)
    if not removed:
        return url
    return (prefix + ('?' + '&'.join(remaining) if remaining else '')
        + fragment_separator + fragment)


def origin(url: str, *, allow_loopback: bool = False) -> str:
    parsed = urlsplit(url)
    if "\\" in url or any(ord(c) <= 32 for c in url) or parsed.username or parsed.password:
        raise PermissionError("browser_origin_invalid")
    if parsed.scheme == "https" and parsed.hostname:
        return parsed.scheme + "://" + parsed.netloc
    # Loopback HTTP is test-only. Production manifests and the device worker
    # never enable it; Supymem's own bridge also listens on loopback.
    if (allow_loopback and parsed.scheme == "http"
            and parsed.hostname in {"127.0.0.1", "localhost"} and parsed.hostname):
        host = parsed.hostname
        if parsed.port:
            return f"http://{host}:{parsed.port}"
        return f"http://{host}"
    raise PermissionError("browser_origin_invalid")


def reviewed_read_url_origin(value, *, allow_loopback=False):
    """An exact browser-representable GET literal, never a normalized substitute."""
    from pydantic import AnyHttpUrl
    if (not isinstance(value, str) or not 8 <= len(value) <= 500 or '?' in value or '#' in value
            or '\\' in value or any(ord(char) <= 32 for char in value)
            or urlsplit(value).username is not None or urlsplit(value).password is not None):
        raise PermissionError('browser_exact_read_urls_required')
    try:
        if str(AnyHttpUrl(value)) != value:
            raise ValueError('noncanonical_url')
        return origin(value, allow_loopback=allow_loopback)
    except ValueError:
        raise PermissionError('browser_exact_read_urls_required') from None


def _control_snapshot(handle) -> dict:
    return handle.evaluate(
        """el => {
          const label = (el.getAttribute('aria-label') || el.labels?.[0]?.innerText
            || el.getAttribute('placeholder') || el.innerText || el.value || '').trim().slice(0,300);
          const tag = el.tagName;
          const inputType = (el.getAttribute('type') || '').toLowerCase();
          const autocomplete = (el.getAttribute('autocomplete') || '').toLowerCase();
          const name = (el.getAttribute('name') || '').toLowerCase();
          const role = (el.getAttribute('role') || '').toLowerCase();
          const href = el.href || null;
          const form = !!el.closest('form');
          const submit = inputType === 'submit' || el.getAttribute('type') === 'submit'
            || (tag === 'BUTTON' && form && inputType !== 'button');
          const terms = inputType === 'checkbox' && /terms|agree|privacy|tos/i.test(
            label + ' ' + name + ' ' + (el.closest('label')?.innerText || ''));
          const opaqueActivation=['onclick','onkeydown','onkeyup','onkeypress','oninput','onchange',
            'onpointerdown','onmousedown'].some(name=>el.hasAttribute(name));
          return {label, href, form, tag, input_type: inputType, autocomplete, name, role,
                  submit, terms_checkbox: terms, captcha: false, opaque_activation:opaqueActivation,
                  has_value: typeof el.value==='string' && !!el.value.trim()};
        }"""
    )


def _observed_handles(document, selector, limit):
    # ponytail: 6,000 DOM nodes per observation, use bounded document reads/scroll for larger pages.
    snapshot = document.evaluate_handle(
        """([selector, limit]) => {
          const matches=[]; let scanned=0;
          const walkers=[document.createTreeWalker(document,NodeFilter.SHOW_ELEMENT)];
          while(walkers.length && matches.length < limit && scanned<6000){
            const element=walkers[walkers.length-1].nextNode();
            if(!element){walkers.pop();continue;}
            scanned++;
            if(element.matches(selector))matches.push(element);
            if(element.shadowRoot)walkers.push(document.createTreeWalker(element.shadowRoot,NodeFilter.SHOW_ELEMENT));
          }
          return matches;
        }""",
        [selector, limit],
    )
    try:
        return [handle.as_element() for key, handle in snapshot.get_properties().items() if key.isdecimal()]
    finally:
        snapshot.dispose()


class BrowserPreDispatchRefused(PermissionError):
    """Source knows no input began after an admitted browser effect."""

    def __init__(self, code):
        if code not in {
            "browser_pre_dispatch_fresh_observation_required",
            "browser_pre_dispatch_target_changed",
            "browser_pre_dispatch_scope_retired",
        }:
            raise ValueError("browser_pre_dispatch_code_invalid")
        self.pre_dispatch_code = code
        super().__init__(code)


class OwnedBrowser:
    def __init__(self, *, order_id, goal, origins, authorize, admit_effect,
                 process_observer=None, general_mode=False, fill_values=None,
                 profile_dir=None, allow_loopback_for_tests: bool = False, cuai_mode: bool = False,
                 chrome_factory=None, cuai_version="browser_cuai_text_v1", read_only=False,
                 qualification_grant=None, qualification_current=None, qualification_gateway=None,
                 bounded_reads: bool = False, read_urls=()):
        if not order_id or not callable(authorize) or not callable(admit_effect):
            raise PermissionError("browser_host_authority_required")
        self._allow_loopback = bool(allow_loopback_for_tests)
        if not origins or any(
                origin(value, allow_loopback=self._allow_loopback) != value for value in origins):
            raise PermissionError("browser_exact_origins_required")
        self.order_id, self.goal, self.origins = order_id, goal, frozenset(origins)
        self.authorize, self.admit_effect = authorize, admit_effect
        self.process_observer = process_observer if callable(process_observer) else lambda: None
        self.general_mode = bool(general_mode)
        if type(cuai_mode) is not bool or cuai_mode and not self.general_mode:
            raise PermissionError('browser_cuai_scope_invalid')
        self.cuai_mode = cuai_mode
        if type(bounded_reads) is not bool or bounded_reads and not cuai_mode:
            raise PermissionError('browser_cuai_scope_invalid')
        self.bounded_reads = bounded_reads
        if type(read_only) is not bool or read_only and (not cuai_mode or fill_values):
            raise PermissionError('browser_cuai_scope_invalid')
        self.read_only = read_only
        if (not isinstance(read_urls, (tuple, list)) or len(read_urls) > 6 or any(not isinstance(url, str) for url in read_urls)
                or len(set(read_urls)) != len(read_urls)
                or read_urls and (not read_only or not bounded_reads or qualification_grant is not None)):
            raise PermissionError('browser_exact_read_urls_required')
        for url in read_urls:
            if reviewed_read_url_origin(url, allow_loopback=self._allow_loopback) not in self.origins:
                raise PermissionError('browser_exact_read_urls_required')
        self.read_urls = tuple(read_urls)
        self._reviewed_read_navigation = None
        self.qualification_grant = None
        self._qualification_current = None
        self._qualification_gateway = qualification_gateway
        self._qualification_pending = None
        self._qualification_proposal = None
        self.last_qualification_response = None
        self.last_qualification_download = None
        self.last_qualification_read_response = None
        self._qualification_download_pending = None
        self._qualification_download_dir = None
        self._qualification_owned_pages = set()
        self._qualification_opening = None
        self._qualification_frames = {}
        self._qualification_observed_frames = []
        self._qualification_redirect = None
        if qualification_grant is not None:
            from core.browser_qualification import OwnedFixtureGrant
            grant = OwnedFixtureGrant.from_dict(qualification_grant)
            if (not cuai_mode or not callable(qualification_current) or grant.order_id != order_id
                    or grant.origin not in self.origins):
                raise PermissionError("browser_qualification_scope_invalid")
            self.qualification_grant = grant
            self._qualification_current = qualification_current
        if cuai_version not in {"browser_cuai_text_v1", "browser_cuai_text_v2"} or cuai_version == "browser_cuai_text_v2" and not cuai_mode:
            raise PermissionError('browser_cuai_scope_invalid')
        self.cuai_version = cuai_version
        self._cuai_reads = []
        self._cuai_reads_omitted = 0
        self._cuai_read_document = None
        self._cuai_pages = []
        self._cuai_inputs = []
        self._cuai_omitted = {'pages':0,'inputs':0}
        self._cuai_current = None
        self.fill_values = {
            key: value for key, value in (fill_values or {}).items()
            if isinstance(key, str) and isinstance(value, str) and key in {"email", "name", "text", "tel"}
            and 0 < len(value) <= 320
        }
        if self.cuai_mode and any(_cuai_redact(value)!=value for value in self.fill_values.values()):
            raise PermissionError('browser_cuai_sensitive_fill_refused')
        self.session_id = uuid4().hex
        self._playwright = self._browser = self._context = self._page = None
        self._observation = None
        self._handles = {}
        self._control_meta = {}
        self._uncertain = False
        self.last_read_refusal = None
        self._expected_location = None
        self._headed = False
        self._route_authority_until = 0.0
        # chrome_factory (cu_ai_browser.ChromeFactory with account_id): the persistent per-account Supymem
        # browser. Chrome is started and killed by the factory, which never deletes that profile.
        self._factory = chrome_factory
        self._factory_headless = bool(chrome_factory.headless) if chrome_factory is not None else None
        self._temporary_profile = (tempfile.TemporaryDirectory(prefix=f"supymem-owned-{order_id[:12]}-")
                                   if chrome_factory is None and not profile_dir else None)
        self._profile_dir = (None if chrome_factory is not None else Path(profile_dir) if profile_dir
                             else Path(self._temporary_profile.name))
        if self._profile_dir is not None:
            self._profile_dir.mkdir(parents=True, exist_ok=True)
        self.pause_kind = None
        self.founder_prompt = None
        self.last_typed_label = None
        self.last_control_summary = {"click": 0, "type": 0, "signup_links": 0}
        self._load_pages = {}
        self._load_unbound_requests = set()
        self._load_document_key = '__supymem_document_' + self.session_id
        self.last_load_evidence = None

    def _watch_load(self, page):
        """Bind completed HTTP navigation to this page's current document generation.

        Only main-document requests count; images/XHR/iframe responses cannot
        supply a successful load. A new request invalidates prior success before
        its result is known, including same-URL reloads and failed deliveries.
        """
        if page in self._load_pages:
            return
        self._load_pages[page] = {'page_id': uuid4().hex, 'sequence': 0, 'committed_sequence': None,
                                  'request': None, 'response': None, 'document_id': None,'qualification_response':None}

        def main_request(request):
            try:
                return request.is_navigation_request() and request.frame == page.main_frame
            except Exception:
                return False

        def requested(request):
            if main_request(request):
                record = self._load_pages[page]
                record.update(sequence=record['sequence'] + 1, committed_sequence=None,
                              request=request, response=None, document_id=None,qualification_response=None)
                self.last_load_evidence = None
                self.last_read_refusal = None

        def committed(frame):
            if frame == page.main_frame:
                record = self._load_pages[page]
                record['committed_sequence'] = record['sequence']
                record['document_id'] = None
                self.last_load_evidence = None
            elif self.qualification_grant is not None:
                record = self._qualification_frames.get(frame)
                if record is not None:
                    record.update(committed_sequence=record['sequence'], document_id=None)

        def finished(request):
            record = self._load_pages[page]
            if request != record['request'] or not main_request(request):
                return
            try:
                response = request.response()
                record['response'] = ({'url': response.url, 'status': response.status,
                    'sequence': record['sequence'], 'complete': request.failure is None}
                    if response is not None else None)
                if self.qualification_grant is not None and response is not None and 400 <= response.status <= 599:
                    data = response.body()
                    request_id = response.headers.get('x-supymem-qualification-request', '')
                    if isinstance(data, bytes) and len(data) <= 65536 and re.fullmatch(r'bqr_[A-Za-z0-9_-]{1,128}',request_id):
                        record['response'].update(request_id=request_id,sha256=hashlib.sha256(data).hexdigest(),
                            size_bytes=len(data),mime_type=response.headers.get('content-type','').split(';',1)[0].strip().lower())
            except Exception:
                record['response'] = None

        def failed(request):
            record = self._load_pages[page]
            if request == record['request']:
                record['response'] = None
                self.last_load_evidence = None

        # Context request events precede popup Page events. Keep initial popup
        # navigation custody rather than guessing from its final URL later.
        self._load_pages[page]['handlers'] = {'request': requested,
            'requestfinished': finished, 'requestfailed': failed}
        page.on('framenavigated', committed)

    def _load_event(self, kind, request):
        try:
            page = request.frame.page
            if not request.is_navigation_request():
                return
            if request.frame != page.main_frame:
                if getattr(self,'qualification_grant',None) is not None:
                    self._qualification_frame_event(kind, request)
                return
            self._watch_load(page)
            record = self._load_pages[page]
            if (kind == 'requestfinished' and request in self._load_unbound_requests
                    and record['sequence'] == 0 and record['committed_sequence'] == 0
                    and record['request'] is None):
                # Popup requests can precede creation of their Frame. The exact
                # context-observed request is recovered only for that page's
                # initial committed generation, never from a final URL alone.
                record.update(sequence=1, committed_sequence=1, request=request)
            self._load_pages[page]['handlers'][kind](request)
            if kind != 'request':
                self._load_unbound_requests.discard(request)
        except Exception:
            # Unknown early/closed frame events can never create load proof.
            self.last_load_evidence = None
            try:
                if (kind == 'request' and request.is_navigation_request()
                        and self._origin(request.url) in self.origins
                        and len(self._load_unbound_requests) < 32):
                    self._load_unbound_requests.add(request)
                elif kind != 'request':
                    self._load_unbound_requests.discard(request)
            except Exception:
                pass

    def verified_load(self, observation: Observation) -> bool:
        """A fresh successful visible document, never business/task semantics."""
        proof = self.last_load_evidence
        record = self._load_pages.get(self._page)
        return bool(isinstance(proof, dict) and record is not None
            and proof.get('qualified') is True
            and proof.get('observation_digest') == observation.digest()
            and proof.get('session_id') == self.session_id
            and isinstance(proof.get('page_id'), str) and len(proof['page_id']) == 32
            and all(char in '0123456789abcdef' for char in proof['page_id'])
            and proof.get('page_id') == record.get('page_id')
            and proof.get('location') == observation.location == self._page.url
            and proof.get('sequence') == record['sequence'] == record['committed_sequence']
            and proof.get('document_id') == record.get('document_id')
            and record.get('response') == proof.get('response') and not self._uncertain)

    def qualification_recoverable_load(self, observation):
        """Exact known HTTP error document; it is never successful-load evidence."""
        proof, record = self.last_load_evidence, self._load_pages.get(self._page) or {}
        response = record.get('response') or {}
        return bool(self.qualification_grant is not None and not self._uncertain and observation is self._observation
            and isinstance(proof,dict) and proof.get('qualified') is False
            and proof.get('observation_digest') == observation.digest()
            and proof.get('session_id') == self.session_id and proof.get('page_id') == record.get('page_id')
            and proof.get('location') == observation.location == self._page.url
            and proof.get('sequence') == record.get('sequence') == record.get('committed_sequence')
            and proof.get('document_id') == record.get('document_id')
            and proof.get('ready_state') == 'complete' and proof.get('visible_text') is True
            and proof.get('response') == response and response.get('complete') is True
            and type(response.get('status')) is int and 400 <= response['status'] <= 599
            and response.get('url') == observation.location and response.get('sequence') == proof.get('sequence')
            and isinstance(response.get('request_id'),str) and re.fullmatch(r'bqr_[A-Za-z0-9_-]{1,128}',response['request_id'])
            and isinstance(response.get('sha256'),str) and re.fullmatch(r'[a-f0-9]{64}',response['sha256'])
            and type(response.get('size_bytes')) is int and 0 <= response['size_bytes'] <= 65536
            and response.get('mime_type') == 'text/html')

    def qualification_payment_fact(self,observation):
        """One current non-secret total fact, only after its exact GET receipt is delivered."""
        if (self.qualification_grant is None or self.pause_kind != 'payment' or not self.verified_load(observation)
                or self._cuai_current is None or self._cuai_current.get('observation_digest') != observation.digest()):
            return None
        record = self._load_pages.get(self._page) or {}
        response = record.get('qualification_response')
        if (not isinstance(response,dict) or response.get('url') != observation.location
                or response.get('sequence') != record.get('sequence') or response.get('status') != 200
                or response.get('mime_type') != 'text/html'):
            return None
        for _ in range(20):
            self._check()
            if not self.verified_load(observation):
                return None
            receipt = self._qualification_gateway.qualification_read_receipt(response,allow_pending=True)
            if receipt.get('state') != 'committed':
                return None
            if receipt.get('server_response_sent') is True:
                facts = self._cuai_current.get('source',{}).get('facts',[])
                totals = [fact for fact in facts if isinstance(fact,str) and len(fact)<=120
                    and re.fullmatch(r'(?:(?:order|cart) )?total: \$[0-9]{1,9}(?:\.[0-9]{1,2})?',fact,re.I)]
                return totals[0] if len(totals)==1 else None
            self._page.wait_for_timeout(100)
        return None

    def reviewed_navigation_offer(self, observation: Observation, expected_location: str) -> dict | None:
        """Source observation only; Core must record the proposal before any input.

        Resolve only an exact reviewed URL from the current offered controls.
        Labels, page instructions and model answers cannot provide that URL.
        This does not authorize a click or add another DOM observation.
        """
        if (self.general_mode or self._observation is None
                or self._observation.digest() != observation.digest()
                or not self.verified_load(observation)
                or not isinstance(expected_location, str) or not 0 < len(expected_location) <= 500):
            return None
        try:
            if self._origin(expected_location) not in self.origins:
                return None
        except (ValueError, PermissionError):
            return None
        snapshots, matching = [], []
        for control in observation.controls:
            original = self._control_meta.get(control.id)
            if original is None or control.id not in self._handles:
                return None
            item = {'id': control.id, 'tag': original.get('tag'),
                'href': original.get('href'), 'form': original.get('form') is True,
                'visible': control.visible, 'enabled': control.enabled}
            snapshots.append(item)
            if (item['tag'] == 'A' and item['form'] is False and item['visible'] is True
                    and item['enabled'] is True and 'click' in control.operations
                    and strip_check_marker(item['href']) == expected_location):
                matching.append(control.id)
        if len(matching) != 1 or len(snapshots) > 100:
            return None
        proof = self.last_load_evidence
        keys = ('qualified', 'session_id', 'page_id', 'sequence', 'location', 'document_id',
                'ready_state', 'visible_text', 'visibility_scope', 'observation_digest')
        current_load = {key: proof.get(key) for key in keys}
        current_load['response'] = dict(proof['response'])
        return {'target': matching[0], 'current_load': current_load,
                'control_snapshots': snapshots}

    def _origin(self, url: str) -> str:
        return origin(url, allow_loopback=self._allow_loopback)

    def _check(self):
        if self._uncertain or self.authorize(self.order_id, self.session_id) is not True:
            raise PermissionError("browser_current_authority_required")
        if self._page and self._page.url != "about:blank" and self._origin(self._page.url) not in self.origins:
            raise PermissionError("browser_location_out_of_scope")
        grant = getattr(self, "qualification_grant", None)
        if grant is not None:
            current = self._qualification_current()
            if (type(current) is not type(grant) or current != grant
                    or int(time.time()) >= grant.expires_at):
                raise PermissionError("browser_qualification_authority_required")
            if self._page is not None and self._qualification_owned_pages and self._page not in self._qualification_owned_pages:
                raise PermissionError("browser_qualification_tab_unreviewed")

    def _qualification_frame_scope(self, frame, url=None):
        """Only a reviewed direct child of this owned page may supply source content."""
        grant = self.qualification_grant
        if (grant is None or frame.page not in self._qualification_owned_pages
                or frame.parent_frame != frame.page.main_frame):
            return None
        location = url or frame.url
        if self._origin(location) != grant.origin:
            return None
        element = frame.frame_element()
        source = element.evaluate("""el => ({id:el.id,src:el.src,parent:el.ownerDocument.location.pathname,
            parent_document_id:el.ownerDocument.body?.id || '',
            unique:!!el.id && Array.from(el.ownerDocument.querySelectorAll('[id]')).filter(n=>n.id===el.id).length===1
                && !!el.ownerDocument.body?.id && Array.from(el.ownerDocument.querySelectorAll('[id]')).filter(n=>n.id===el.ownerDocument.body.id).length===1})""")
        parent = urlsplit(frame.page.url)
        if not any(rule.method == 'GET' and rule.path == parent.path and rule.query == parent.query
                and rule.document_id == source['parent_document_id'] for rule in grant.rules):
            return None
        return next((item for item in grant.frames if source['unique'] is True
            and item.frame_id == source['id'] and item.parent_document_path == source['parent']
            and item.document_path == urlsplit(location).path
            and source['src'] == grant.origin + item.document_path), None)

    def _qualification_frame_event(self, kind, request):
        frame = request.frame
        if self._qualification_frame_scope(frame, request.url) is None:
            return
        record = self._qualification_frames.setdefault(frame, {'sequence':0, 'committed_sequence':None,
            'request':None, 'response':None, 'document_id':None})
        if kind == 'request':
            record.update(sequence=record['sequence'] + 1, committed_sequence=None,
                request=request, response=None, document_id=None)
        elif request == record['request']:
            response = request.response() if kind == 'requestfinished' else None
            record['response'] = ({'url':response.url, 'status':response.status,
                'sequence':record['sequence'], 'complete':request.failure is None} if response is not None else None)

    def _qualification_reviewed_frames(self):
        result = []
        if self.qualification_grant is None:
            return result
        for frame, record in list(self._qualification_frames.items()):
            try:
                if frame.page is not self._page:
                    continue
                scope = self._qualification_frame_scope(frame)
                response = record['response']
                if (scope is None or not isinstance(response, dict) or response['complete'] is not True
                        or not 200 <= response['status'] < 300 or response['status'] in {204,205}
                        or response['url'] != frame.url or response['sequence'] != record['sequence']
                        or record['committed_sequence'] != record['sequence']):
                    continue
                source = frame.evaluate('''key => ({document_id:document[key]||null,
                    source_document_id:document.body?.id || '',
                    unique:!!document.body?.id && Array.from(document.querySelectorAll('[id]')).filter(n=>n.id===document.body.id).length===1,
                    ready_state:document.readyState,source:(SOURCE)([])})'''.replace('SOURCE', _CUAI_SOURCE_JS), self._load_document_key)
                if (not source['document_id'] or source['ready_state'] != 'complete'
                        or source['unique'] is not True or not any(rule.method == 'GET'
                            and rule.path == scope.document_path and rule.document_id == source['source_document_id']
                            for rule in self.qualification_grant.rules)
                        or record['document_id'] not in {None, source['document_id']}):
                    continue
                record['document_id'] = source['document_id']
                result.append((frame, scope, source['source']))
            except Exception:
                continue
        return result

    def _snapshot(self, handle):
        info = _control_snapshot(handle)
        if getattr(self, "qualification_grant", None) is not None:
            source = handle.evaluate("""el => {
                const container=el.closest('form') || el.closest('[data-qualification-container]');
                const unique=id=>!!id && Array.from(el.ownerDocument.querySelectorAll('[id]')).filter(n=>n.id===id).length===1;
                return {source_control_id:el.id,source_document_id:el.ownerDocument.body?.id || '',
                    source_container_id:container?.id || '',source_document_path:el.ownerDocument.location.pathname,
                    source_frame_id:el.ownerDocument.defaultView.frameElement?.id || '',
                    source_unique:unique(el.id) && unique(container?.id || '') && unique(el.ownerDocument.body?.id || ''),
                    source_input_type:el.tagName==='TEXTAREA'?'textarea':(el.type || '').toLowerCase(),
                    source_value:el.value || '',source_checked:el.checked===true};
            }""")
            info.update(source)
        return info

    def _qualification_status(self):
        if self.qualification_grant is None:
            return None
        method = getattr(self._qualification_gateway, "qualification_order_status", None)
        if not callable(method):
            raise PermissionError("browser_qualification_status_unconfirmed")
        value = method()
        if not isinstance(value, dict) or value.get("grant_digest") != self.qualification_grant.grant_digest:
            raise PermissionError("browser_qualification_status_unconfirmed")
        return value

    def _qualification_rules(self, info, status):
        grant = self.qualification_grant
        if grant is None or status is None:
            return []
        if info.get("source_frame_id") and not any(
                frame.frame_id == info["source_frame_id"] and frame.document_path == info.get("source_document_path")
                and frame.parent_document_path == urlsplit(self._page.url).path for frame in grant.frames):
            return []
        result = []
        for rule in grant.rules:
            count = status["rules"][rule.rule_id]
            if (info.get("source_unique") is True and rule.document_path == info.get("source_document_path") and rule.document_id == info.get("source_document_id")
                    and rule.form_id == info.get("source_container_id")
                    and set(rule.prerequisites) <= set(status["completed_rules"])
                    and count["protected"] is False and count["request_count"] < rule.max_requests
                    and (rule.max_commits == 0 or count["committed_count"] < rule.max_commits)):
                result.append(rule)
        return result

    def _qualification_link_allowed(self, href, status):
        try:
            rule = self.qualification_grant.request_rule(url=href, method="GET",
                completed_rules=status.get("read_prerequisites",status["completed_rules"]))
            count = status["rules"][rule.rule_id]
            return count["protected"] is False and count["request_count"] < rule.max_requests
        except (ValueError, KeyError, TypeError):
            return False

    def _qualification_field(self, info, status, operation):
        grant = self.qualification_grant
        if grant is None:
            return None
        for rule in self._qualification_rules(info, status):
            for field in rule.fields:
                if field.control_id != info.get("source_control_id"):
                    continue
                secret_target = bool(re.search(r"password|passwd|secret|token|otp|captcha|card|cvc|cvv|terms|account",
                    str(info.get("name") or "") + " " + str(info.get("autocomplete") or ""), re.I)
                    or str(info.get("autocomplete") or "").startswith(("cc-", "one-time-code")))
                if operation == "type" and "type" in grant.allowed_operations and field.input_value is not None and not secret_target:
                    if (info.get("source_input_type") == field.input_type and info.get("tag") in {"INPUT", "TEXTAREA"}
                            and (is_safe_type_target(info) or info.get("tag") == "TEXTAREA" and classify_control(info) is None
                                or field.kind in {"synthetic_code", "synthetic_billing_contact"} and field.input_type in {"text", "email"})
                            and not info.get("terms_checkbox")):
                        return field
                if (operation == "type" and "select" in grant.allowed_operations and field.kind == "select"
                        and info.get("tag") == "SELECT"):
                    return field
                if (operation == "click" and "check" in grant.allowed_operations and field.kind in {"checkbox", "radio"}
                        and info.get("tag") == "INPUT" and info.get("source_input_type") == field.kind
                        and not info.get("terms_checkbox") and (field.kind != "radio" or info.get("source_value") == field.value)):
                    return field
        return None

    def qualification_context(self, observation):
        if self.qualification_grant is None:
            return None
        self._check()
        if observation is not self._observation or not (self.verified_load(observation) or self.qualification_recoverable_load(observation)):
            raise PermissionError("browser_qualification_observation_changed")
        status = self._qualification_status()
        if any(row["protected"] is True for row in status["rules"].values()):
            raise EffectUncertain("browser_qualification_request_unconfirmed")
        if self.qualification_recoverable_load(observation):
            return {'grant_digest':self.qualification_grant.grant_digest,'controls':[],'tabs':[],'frames':[],'links':[]}
        controls = []
        for control in observation.controls:
            info = self._control_meta.get(control.id) or {}
            field = self._qualification_field(info, status, "type") or self._qualification_field(info, status, "click")
            controls.append({"id": control.id, "document_path": info.get("source_document_path", ""),
                "document_id": info.get("source_document_id", ""), "container_id": info.get("source_container_id", ""),
                "control_id": info.get("source_control_id", ""), "tag": info.get("tag", ""),
                "input_type": info.get("source_input_type") or None, "input_value": field.input_value if field else None,
                "kind": field.kind if field else None,
                "hover_only":info.get('source_hover_only') is True,
                "select_value": field.value if field and field.kind in {"select", "radio", "checkbox"} else None})
        return {"grant_digest": self.qualification_grant.grant_digest, "controls": controls,
            "tabs": self._qualification_tabs(status),
            "frames": list(self._qualification_observed_frames),
            "links": [{"id": link["id"], "href": link["href"]}
                for link in self.cuai_navigation_targets(observation)]}

    def _qualification_tabs(self, status):
        tabs = []
        for page in self._context.pages:
            if page not in self._qualification_owned_pages or page.is_closed():
                continue
            record = self._load_pages.get(page) or {}
            response = record.get('response')
            try:
                self.qualification_grant.request_rule(url=page.url, method='GET',
                    completed_rules=status['completed_rules'])
                if (isinstance(response, dict) and response['complete'] is True
                        and response['url'] == page.url and 200 <= response['status'] < 300
                        and response['status'] not in {204,205}
                        and record['sequence'] == record['committed_sequence'] == response['sequence']):
                    tabs.append({'id':'t' + record['page_id'], 'url':page.url})
            except (ValueError, KeyError, TypeError):
                continue
        return tabs

    def qualification_proposal(self, invocation_id, candidate, rule_ids):
        if self.qualification_grant is None:
            if rule_ids:
                raise PermissionError("browser_qualification_proposal_invalid")
            return
        if (not isinstance(rule_ids, (tuple, list)) or any(not isinstance(key, str) for key in rule_ids)
                or len(set(rule_ids)) != len(rule_ids) or len(rule_ids) > 128
                or not set(rule_ids) <= {rule.rule_id for rule in self.qualification_grant.rules if rule.method == "POST"}
                or self._observation is None or candidate.observation_digest != self._observation.digest()):
            raise PermissionError("browser_qualification_proposal_invalid")
        self._qualification_proposal = (invocation_id, candidate.observation_digest, tuple(rule_ids))

    def _qualification_post(self, route):
        from core.browser_qualification import request_binding_digest
        pending = getattr(self, "_qualification_pending", None)
        if (pending is None or pending["locked"] or route.request.resource_type not in {"document", "fetch", "xhr"}
                or route.request.frame.page is not self._page):
            raise PermissionError("browser_qualification_request_invalid")
        before = pending["binding"]
        if route.request.is_navigation_request():
            # DOM is checked immediately before the admitted click. Evaluating
            # it during a paused navigation would block behind that request.
            # No replacement document may commit in this source event interval.
            record = self._load_pages.get(self._page) or {}
            expected_navigation = (self._page is before[0] and self._page.url == before[1]
                and record.get("page_id") == before[2] and route.request.frame == self._page.main_frame
                and record.get("request") == route.request
                and record.get("sequence") == before[3] + 1 and record.get("committed_sequence") is None)
            if not expected_navigation:
                raise PermissionError("browser_qualification_request_invalid")
        elif self._input_binding(pending["handle"]) != before:
            raise PermissionError("browser_qualification_request_invalid")
        data = route.request.post_data_buffer
        if not isinstance(data, bytes) or len(data) > 16384:
            raise PermissionError("browser_qualification_request_invalid")
        content_type = route.request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type == "application/json":
            def pairs(items):
                if len({key for key, _ in items}) != len(items):
                    raise ValueError("duplicate_fields")
                return dict(items)
            body = json.loads(data.decode("utf-8"), object_pairs_hook=pairs)
            if not isinstance(body, dict):
                raise ValueError("body_shape")
            fields, body_kind = list(body.items()), "json"
        elif content_type == "application/x-www-form-urlencoded":
            fields, body_kind = parse_qsl(data.decode("utf-8"), keep_blank_values=True, strict_parsing=True), "form"
        else:
            raise ValueError("body_type")
        status = self._qualification_status()
        rule = self.qualification_grant.request_rule(url=route.request.url, method="POST",
            submitted_fields=fields, completed_rules=status["completed_rules"])
        if rule.rule_id not in pending["rule_ids"] or rule.body_kind != body_kind:
            raise PermissionError("browser_qualification_request_invalid")
        qualification = {"grant_digest": self.qualification_grant.grant_digest, "rule_id": rule.rule_id,
            "request_id": "bqr_" + uuid4().hex}
        qualification["request_digest"] = request_binding_digest(self.qualification_grant, rule, qualification["request_id"])
        # Freeze before the persistence attempt. An unknown/false bind cannot be redialled.
        pending.update(locked=True, qualification=qualification, body_digest=hashlib.sha256(data).hexdigest())
        if self._qualification_gateway.bind_qualification_effect(qualification,
                invocation_id=pending["invocation"], effect_digest=pending["effect_digest"]) is not True:
            raise EffectUncertain("browser_qualification_request_unconfirmed")
        self._check()
        pending["request"] = route.request
        return {"Content-Type": content_type,
            "X-Supymem-Qualification-Invocation": pending["invocation"],
            "X-Supymem-Qualification-Effect": pending["effect_digest"],
            "X-Supymem-Qualification-Execution": self._qualification_gateway.execution_id,
            "X-Supymem-Qualification-Request": qualification["request_id"]}

    def _qualification_response(self, response):
        download = getattr(self,'_qualification_download_pending',None)
        if download is not None and response.request == download.get('request'):
            download['response'] = {'status':response.status, 'url':response.url,
                'media_type':response.headers.get('content-type', '').split(';', 1)[0].strip().lower(),
                'content_length':response.headers.get('content-length'),
                'request_id':response.headers.get('x-supymem-qualification-request','')}
        pending = getattr(self, "_qualification_pending", None)
        if pending is None or response.request != pending.get("request"):
            return
        if pending.get('intercepted_response'):
            return
        try:
            if response.status == 204:
                # RFC 9110: 204 is complete at the end of its headers. Chrome cannot
                # retrieve its body, and an unconsumed fetch may never emit requestfinished.
                if response.request.failure is not None:
                    raise ValueError("response_unconfirmed")
                data = b""
            else:
                data = response.body()
            if not isinstance(data, bytes) or len(data) > 65536:
                raise ValueError("response_bounds")
            pending.update(response_digest=hashlib.sha256(data).hexdigest(),status_code=response.status,
                response_size_bytes=len(data),response_mime_type=getattr(response,'headers',{}).get(
                    'content-type','').split(';',1)[0].strip().lower())
        except Exception:
            self._uncertain = True

    def _qualification_download_rule(self, url, status):
        grant = self.qualification_grant
        if grant is None or 'download' not in grant.allowed_operations:
            return None
        try:
            rule = grant.request_rule(url=url, method='GET', completed_rules=status['completed_rules'])
            download = next((item for item in grant.downloads if item.rule_id == rule.rule_id), None)
            count = status['rules'][rule.rule_id]
            return download if download is not None and count['protected'] is False and count['request_count'] < rule.max_requests else None
        except (ValueError, TypeError, KeyError):
            return None

    def _qualification_download_event(self, page, download):
        pending = self._qualification_download_pending
        if (pending is None or pending.get('download') is not None or pending['page'] is not page
                or pending['url'] != download.url):
            self._uncertain = True
            download.cancel()
            return
        pending['download'] = download

    def _qualification_download_finish(self):
        pending = self._qualification_download_pending
        if pending is None:
            return False
        response = (self._load_pages.get(pending['page']) or {}).get('response') or {}
        if (pending.get('download') is None and response.get('url') == pending['url']
                and type(response.get('status')) is int and 400 <= response['status'] <= 599
                and response.get('request_id') and response.get('mime_type') == 'text/html'):
            receipt = self._qualification_gateway.qualification_read_receipt(response,allow_pending=True)
            if receipt.get('state') != 'failed_read' or type(receipt.get('server_response_sent')) is not bool:
                raise EffectUncertain('browser_qualification_request_unconfirmed')
            if receipt['server_response_sent'] is False:
                return False
            self.last_qualification_read_response = dict(response)
            self._qualification_download_pending = None
            return True
        if pending.get('download') is None:
            return False
        download = pending['download']
        retain_native = False
        try:
            self._check()
            contract, response = pending['contract'], pending.get('response')
            if (not isinstance(response, dict) or response['status'] != 200 or response['url'] != pending['url']
                    or response['media_type'] != contract.media_type
                    or response['content_length'] is not None and response['content_length'] != str(contract.max_bytes)
                    or download.suggested_filename != contract.filename or download.failure() is not None):
                raise ValueError('download_response_unreviewed')
            source = download.path()
            if source is None or source.stat().st_size != contract.max_bytes:
                raise ValueError('download_size_unreviewed')
            with source.open('rb') as stream:
                data = stream.read(contract.max_bytes + 1)
            if len(data) != contract.max_bytes or hashlib.sha256(data).hexdigest() != contract.sha256:
                raise ValueError('download_digest_unreviewed')
            rows = list(csv.reader(io.StringIO(data.decode('utf-8', errors='strict'))))
            source_proof = {'url':pending['url'],'status':200,'complete':True,
                'sequence':self._load_pages[pending['page']]['sequence'],'request_id':response['request_id'],
                'sha256':contract.sha256,'size_bytes':len(data),'mime_type':contract.media_type}
            receipt = self._qualification_gateway.qualification_read_receipt(source_proof,allow_pending=True)
            if receipt.get('state') != 'committed' or type(receipt.get('server_response_sent')) is not bool:
                raise EffectUncertain('browser_qualification_request_unconfirmed')
            if receipt['server_response_sent'] is False:
                retain_native = True
                return False
            if self._qualification_download_dir is None:
                self._qualification_download_dir = tempfile.TemporaryDirectory(prefix='supymem-fixture-download-')
            download.save_as(Path(self._qualification_download_dir.name) / contract.filename)
            evidence = {'rule_id':contract.rule_id, 'url':pending['url'],
                'filename':contract.filename, 'media_type':contract.media_type, 'size_bytes':len(data),
                'sha256':contract.sha256, 'rows_excluding_header':max(0, len(rows) - 1),
                'invocation_id':pending['invocation'], 'effect_digest':pending['effect_digest'],
                'observation_digest':pending['observation_digest'], 'delivery':'input_delivered',
                'request_id':response['request_id']}
            previous = pending.get('load_before')
            record = self._load_pages[pending['page']]
            if (previous is None or pending['page'].url != pending['source_url']
                    or record['committed_sequence'] is not None
                    or pending['page'].evaluate('key => document[key]',self._load_document_key) != previous['document_id']):
                raise ValueError('download_source_document_changed')
            # A confirmed attachment never replaced the observed document. Keep
            # its original successful/error witness rather than inventing aCSV load.
            record.update(previous)
            self.last_qualification_download = evidence
            self._qualification_download_pending = None
            return True
        finally:
            # Native temporary artifacts never become model-chosen filesystem authority.
            if not retain_native:
                download.delete()

    def _qualification_finish(self):
        pending = getattr(self, "_qualification_pending", None)
        if pending is None:
            return
        for _ in range(20):
            self._check()
            if pending.get("request") is not None and pending.get("response_digest") is not None:
                receipt = self._qualification_gateway.qualification_receipt(pending["qualification"],
                    invocation_id=pending["invocation"], effect_digest=pending["effect_digest"])
                if receipt["state"] in {"committed", "rejected_before_mutation"}:
                    if (receipt["body_digest"] != pending["body_digest"] or receipt["response_digest"] != pending["response_digest"]
                            or receipt["status_code"] != pending["status_code"]
                            or type(receipt.get('response_size_bytes')) is not int
                            or receipt.get('response_size_bytes') != pending.get('response_size_bytes')
                            or receipt.get('response_mime_type') != pending.get('response_mime_type')):
                        break
                    if receipt["server_response_sent"] is True:
                        self.last_qualification_response = {key: receipt[key] for key in (
                            "request_id", "request_digest", "rule_id", "body_digest", "response_digest", "state", "mutated", "status_code",
                            "response_size_bytes","response_mime_type")}
                        self._qualification_pending = None
                        return
                if receipt["state"] == "unknown":
                    break
            self._page.wait_for_timeout(250)
        self._uncertain = True
        raise EffectUncertain("browser_qualification_request_unconfirmed")

    def _route(self, route):
        headers = None
        try:
            if getattr(self, "qualification_grant", None) is not None and route.request.method == "POST":
                self._check()
                headers = self._qualification_post(route)
            allowed = (route.request.method in {"GET", "HEAD"}
                and route.request.resource_type in {
                    "document", "stylesheet", "script", "image", "font", "media"}
                and self._origin(route.request.url) in self.origins)
            if allowed and getattr(self, "read_only", False):
                allowed = self._page is not None and route.request.frame.page is self._page
                if route.request.resource_type == 'document' and getattr(self, '_reviewed_read_navigation', None) is not None:
                    allowed = allowed and route.request.url == self._reviewed_read_navigation
            grant = getattr(self, "qualification_grant", None)
            if allowed and grant is not None:
                # Fixture scope does not grant passive access to its whole origin.
                # Mutation requests stay closed until the durable request/effect path is bound.
                status = self._qualification_status() if self._qualification_gateway is not None else None
                rule = grant.request_rule(url=route.request.url, method=route.request.method,
                    completed_rules=status["completed_rules"] if status is not None else ())
                if status is not None and (status["rules"][rule.rule_id]["protected"] is True
                        or status["rules"][rule.rule_id]["request_count"] >= rule.max_requests):
                    raise PermissionError("browser_qualification_request_limit")
                page, frame = route.request.frame.page, route.request.frame
                main_frame = getattr(page, 'main_frame', frame)
                owned = (not self._qualification_owned_pages and page is self._page) or page in self._qualification_owned_pages
                allowed = (route.request.resource_type == "document" and owned
                    and (frame == main_frame or self._qualification_frame_scope(frame, route.request.url) is not None))
                if any(item.rule_id == rule.rule_id for item in grant.downloads):
                    pending = self._qualification_download_pending
                    if (pending is None or pending['page'] is not page or pending['url'] != route.request.url
                            or pending.get('request') is not None or frame != main_frame):
                        allowed = False
                    else:
                        pending['request'] = route.request
            if headers is not None:
                allowed = True
            # Reject impossible requests before calling the remote authority
            # endpoint. Modern sites fan out to dozens of third-party hosts;
            # serial network checks for requests we will abort can consume the
            # whole document-navigation timeout. In-scope requests still get
            # bounded authority checks before they continue. A one-second
            # positive lease applies only to passive GET/HEAD resources;
            # observations and effects use the uncached authority path.
            if allowed:
                if self._uncertain or (self._page and self._page.url != "about:blank"
                        and self._origin(self._page.url) not in self.origins):
                    raise PermissionError("browser_location_out_of_scope")
                if time.monotonic() >= self._route_authority_until:
                    self._check()
                    self._route_authority_until = time.monotonic() + 1.0
        except Exception:
            allowed = False
            if route.request.method == "POST" and getattr(self, "_qualification_pending", None) is not None:
                self._uncertain = True
        try:
            if allowed:
                if getattr(self,'qualification_grant',None) is not None:
                    self._qualification_continue(route, headers)
                elif (getattr(self, '_reviewed_read_navigation', None) is not None
                        and route.request.resource_type == 'document'):
                    response = route.fetch(max_redirects=0, max_retries=0, timeout=10000)
                    try:
                        if 300 <= response.status < 400:
                            self._uncertain = True
                            route.abort('blockedbyclient')
                            return
                        safe_headers = {key: value for key, value in response.headers.items()
                            if key.lower() not in {'content-encoding', 'content-length'}}
                        route.fulfill(response=response, headers=safe_headers)
                    finally:
                        response.dispose()
                else:
                    self._ordinary_read_continue(route)
            else:
                route.abort("blockedbyclient")
        except Exception as error:
            # Playwright raises this into whatever sync call is waiting (a click, a wait) when the request's frame
            # detached mid-route; the request is gone, so there is nothing to allow or block.
            if "Frame for this navigation request is not available" not in str(error):
                raise

    def _ordinary_read_continue(self, route):
        """No browser-followed redirect can bypass the reviewed origin route."""
        response = None
        refusal = 'browser_read_transport_unconfirmed'
        try:
            response = route.fetch(max_redirects=0, max_retries=0, timeout=10000)
            status, headers = response.status, dict(response.headers)
            if type(status) is not int or not 200 <= status <= 599:
                raise ValueError('response_status_unconfirmed')
            if 300 <= status < 400:
                refusal = 'browser_read_redirect_unavailable'
                raise ValueError('redirect_unavailable')
            length = headers.get('content-length')
            if length is not None and (not length.isdigit() or int(length) > MAX_PASSIVE_BODY_BYTES):
                refusal = 'browser_read_response_over_bound'
                raise ValueError('response_bounds')
            if route.request.method == 'HEAD' or status in {204, 205}:
                data = b''
            else:
                mime = headers.get('content-type', '').split(';', 1)[0].strip().lower()
                if not re.fullmatch(r'[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+', mime):
                    raise ValueError('response_mime_unconfirmed')
                data = response.body()
            if not isinstance(data, bytes) or len(data) > MAX_PASSIVE_BODY_BYTES:
                refusal = 'browser_read_response_over_bound'
                raise ValueError('response_bounds')
            self._check()  # Cancellation/retirement during I/O cannot mint a fresh document.
            safe_headers = {key: value for key, value in headers.items()
                if key.lower() not in {'content-encoding', 'content-length'}}
            if route.request.resource_type == 'document':
                self.last_read_refusal = None
            route.fulfill(status=status, headers=safe_headers, body=data)
        except Exception:
            self.last_read_refusal = refusal
            if route.request.resource_type == 'document':
                self._uncertain = True
            route.abort('blockedbyclient')
        finally:
            if response is not None:
                try:
                    response.dispose()
                except Exception:
                    self.last_read_refusal = 'browser_read_transport_unconfirmed'
                    if route.request.resource_type == 'document':
                        self._uncertain = True

    def _qualification_continue(self, route, headers):
        """Do not let Chrome follow a redirect before the exact next GET is reviewed."""
        response = route.fetch(max_redirects=0,max_retries=0,timeout=10000,
            **({'headers':headers} if headers is not None else {}))
        try:
            response_headers = dict(response.headers)
            status = response.status
            size = response_headers.get('content-length')
            maximum = max((item.max_bytes for item in self.qualification_grant.downloads),default=65536)
            if size is not None and (not size.isdigit() or int(size) > max(maximum,65536)):
                raise ValueError('response_bounds')
            data = response.body()
            if not isinstance(data,bytes) or len(data) > max(maximum,65536):
                raise ValueError('response_bounds')
            if route.request.method == 'GET':
                page = route.request.frame.page
                record = self._load_pages.get(page) or {}
                if route.request.frame == page.main_frame and record.get('request') == route.request:
                    record['qualification_response'] = {'url':route.request.url,'status':status,'complete':True,
                        'sequence':record['sequence'],'request_id':response_headers.get('x-supymem-qualification-request',''),
                        'sha256':hashlib.sha256(data).hexdigest(),'size_bytes':len(data),
                        'mime_type':response_headers.get('content-type','').split(';',1)[0].strip().lower()}
            download = self._qualification_download_pending
            if route.request == (download or {}).get('request'):
                # Chrome may emit Download without a document Response event.
                # This exact no-redirect source response is independently joined
                # to the server receipt and the eventual native file hash.
                download['response'] = {'status':status,'url':route.request.url,
                    'media_type':response_headers.get('content-type','').split(';',1)[0].strip().lower(),
                    'content_length':response_headers.get('content-length'),
                    'request_id':response_headers.get('x-supymem-qualification-request','')}
            pending = self._qualification_pending
            if route.request == (pending or {}).get('request'):
                if not isinstance(data,bytes) or len(data) > 65536:
                    raise ValueError('response_bounds')
                pending.update(response_digest=hashlib.sha256(data).hexdigest(),status_code=status,intercepted_response=True,
                    response_size_bytes=len(data),response_mime_type=response_headers.get('content-type','').split(';',1)[0].strip().lower())
            if 300 <= status < 400:
                if pending is None or route.request != pending.get('request') or status != 303 or data != b'':
                    raise PermissionError('browser_qualification_redirect_unreviewed')
                destination = urljoin(route.request.url,response_headers.get('location',''))
                if not self._qualification_link_allowed(destination,self._qualification_status()):
                    raise PermissionError('browser_qualification_redirect_unreviewed')
                self._qualification_redirect = destination
                # Empty inert document cannot qualify as a successful visible load.
                # The actual303 response is joined before the explicit reviewed GET.
                status = 200
                response_headers = {key:value for key,value in response_headers.items() if key.lower() != 'location'}
            route.fulfill(status=status,headers=response_headers,body=data)
        except Exception:
            self._uncertain = True
            route.abort('blockedbyclient')
            raise
        finally:
            dispose = getattr(response,'dispose',None)
            if callable(dispose):
                dispose()

    def _launch(self, *, headed: bool):
        from playwright.sync_api import sync_playwright
        if self._playwright is None:
            self._playwright = sync_playwright().start()
        self.process_observer()
        if self._factory is not None:
            self._connect_factory(headed)
        else:
            self._launch_playwright(headed)
        self._context.set_default_timeout(5000)
        self._context.route("**/*", self._route)
        self._context.route_web_socket("**/*", lambda socket: socket.close())
        self._context.add_init_script("""for (const key of ['RTCPeerConnection', 'webkitRTCPeerConnection', 'WebTransport']) {
            Object.defineProperty(globalThis,key,{value:undefined,writable:false,configurable:false});
        }""")
        self._context.add_init_script('Object.defineProperty(document,'
            + json.dumps(self._load_document_key)
            + ',{value:crypto.randomUUID(),writable:false,configurable:false});')
        self._context.on("page", self._new_page)
        if self.qualification_grant is not None:
            self._context.on("response", self._qualification_response)
        for kind in ('request', 'requestfinished', 'requestfailed'):
            self._context.on(kind, lambda request, kind=kind: self._load_event(kind, request))
        self._headed = headed
        self.process_observer()

    def _connect_factory(self, headed: bool):
        # A persistent profile can carry service workers and downloads from earlier sessions; the Playwright launch
        # blocks both, so over CDP they are refused explicitly (fail closed) before any page loads.
        factory = self._factory
        factory.headless = False if headed else self._factory_headless
        endpoint = factory.start_endpoint()
        try:
            for attempt in range(4):
                try:
                    self._browser = self._playwright.chromium.connect_over_cdp(endpoint, timeout=4000)
                    break
                except Exception:
                    if attempt == 3:
                        raise
                    time.sleep(0.3)
            self._context = self._browser.contexts[0]
            if self.qualification_grant is None or not self.qualification_grant.downloads:
                self._browser.new_browser_cdp_session().send(
                    "Browser.setDownloadBehavior", {"behavior":"deny", "eventsEnabled":False})
            # For exact fixture downloads keep Playwright's allowAndName custody
            # in its private artifact directory. Replacing it with aCDP filename
            # path breaks the Download artifact identity and cleanup.
            self._context.on("page", self._deny_service_workers)
            for page in self._context.pages[1:]:
                page.close()  # tabs restored from an earlier session
            for page in self._context.pages:
                self._deny_service_workers(page)
        except BaseException:
            self._browser = self._context = None
            try:
                factory.stop()
            except Exception:
                raise RuntimeError("browser_factory_cleanup_unconfirmed") from None
            raise

    def _deny_service_workers(self, page):
        try:
            self._context.new_cdp_session(page).send("Network.setBypassServiceWorker", {"bypass": True})
        except Exception:
            if not page.is_closed():
                raise

    @property
    def visible(self) -> bool:
        return self._factory is not None and not self._factory_headless

    def reveal(self) -> bool:
        """Raise the visible Supymem browser window. Only from the founder's explicit click."""
        factory = self._factory
        return bool(factory is not None and factory.reveal())

    def watch_frame(self):
        """Small JPEG of the current page for the console watch panel; None when nothing is open."""
        if self._page is None:
            return None
        size = self._page.viewport_size or {}
        return {"jpeg": self._page.screenshot(type="jpeg", quality=40), "cursor": None,
                "viewport": {"w": size["width"], "h": size["height"]} if size.get("width") else None}

    def _launch_playwright(self, headed: bool):
        launch_kwargs = dict(
            user_data_dir=str(self._profile_dir),
            channel="chrome",
            headless=not headed,
            accept_downloads=bool(self.qualification_grant is not None and self.qualification_grant.downloads),
            service_workers="block",
            viewport={"width": 1280, "height": 900},
            env={key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR", "LANG") if key in os.environ},
            chromium_sandbox=True,
        )
        if headed:
            launch_kwargs["args"] = ["--disable-blink-features=AutomationControlled"]
        self._browser = self._playwright.chromium.launch_persistent_context(**launch_kwargs)
        self._context = self._browser

    def start(self, url):
        self._check()
        if self._browser is not None or self._origin(url) not in self.origins:
            raise PermissionError("browser_start_invalid")
        stage = "chrome"
        try:
            self._launch(headed=False)
            stage = "page"
            pages = self._context.pages
            self._page = pages[0] if pages else self._context.new_page()
            if self.qualification_grant is not None:
                if self._page not in self._qualification_owned_pages:
                    self._page.on('download', lambda download, page=self._page: self._qualification_download_event(page, download))
                self._qualification_owned_pages.add(self._page)
            self._watch_load(self._page)
            stage = "navigation"
            # Modern signup pages can defer DOMContentLoaded behind slow or
            # blocked third-party scripts. A committed in-scope document with
            # an attached body is enough for our first observation; waiting
            # for every script can turn a usable page into a false failure.
            self._page.goto(url, wait_until="commit", timeout=15000)
            self._page.locator("body").wait_for(state="attached", timeout=5000)
            stage = "authority"
            self._check()
        except Exception as error:
            # Never persist an untrusted page's exception text (which may
            # contain URLs or field values). Keep a bounded diagnostic class
            # so a real Mac failure distinguishes timeouts from network and
            # authority failures without exposing page contents.
            kind = "timeout" if type(error).__name__ == "TimeoutError" else (
                "authority" if isinstance(error, PermissionError) else
                "network" if "net::ERR_" in str(error) else "error")
            self.close()
            raise RuntimeError(f"owned_browser_{stage}_{kind}") from None

    def open_founder_handoff(self):
        """Relaunch the same profile headed so the founder can complete a pause.

        Focus may move to this owned window only during an explicit Needs-you
        handoff. Callers must not use this for background automation.
        """
        if not self.general_mode or getattr(self, "read_only", False):
            raise PermissionError("browser_handoff_requires_general_mode")
        self._check()
        if getattr(self, "qualification_grant", None) is not None:
            raise PermissionError("browser_qualification_handoff_unavailable")
        current = self._page.url if self._page else "about:blank"
        if self._factory is not None and not self._factory_headless:
            self.reveal()  # already a visible (minimized) window on the same profile: no relaunch
            return current
        try:
            if self._browser:
                self._browser.close()
        except Exception:
            pass
        self._stop_factory()
        self._browser = self._context = self._page = None
        self._launch(headed=True)
        pages = self._context.pages
        self._page = pages[0] if pages else self._context.new_page()
        if current and current != "about:blank":
            self._page.goto(current, wait_until="domcontentloaded", timeout=15000)
        self._check()
        return current

    def return_to_background(self):
        """Close the headed handoff window and resume headless on the same profile."""
        if not self.general_mode:
            raise PermissionError("browser_handoff_requires_general_mode")
        self._check()
        if self._factory is not None and not self._factory_headless:
            self.pause_kind = self.founder_prompt = None
            return
        current = self._page.url if self._page else "about:blank"
        try:
            if self._browser:
                self._browser.close()
        except Exception:
            pass
        self._stop_factory()
        self._browser = self._context = self._page = None
        self._launch(headed=False)
        pages = self._context.pages
        self._page = pages[0] if pages else self._context.new_page()
        if current and current != "about:blank":
            self._page.goto(current, wait_until="domcontentloaded", timeout=15000)
        self._check()
        self.pause_kind = None
        self.founder_prompt = None

    def _new_page(self, page):
        grant = getattr(self,'qualification_grant',None)
        if grant is not None:
            opening = self._qualification_opening
            already_owned = page in self._qualification_owned_pages
            if (self._page is not None and not already_owned and (opening is None or opening.get('page') is not None
                    or page.opener() is not None
                    or len([item for item in self._qualification_owned_pages if not item.is_closed()]) >= grant.max_tabs)):
                self._uncertain = True
                page.close()
                return
            self._qualification_owned_pages.add(page)
            if opening is not None:
                opening['page'] = page
            page.on('download', lambda download: self._qualification_download_event(page, download))
            self._watch_load(page)
            return
        if getattr(self, "read_only", False) and self._page is not None and page is not self._page:
            self._uncertain = True  # Refuse before closing; failed close still requires owned-process cleanup.
            page.close()
            return
        if len(self._context.pages) > 4:
            page.close()
            return
        self._page = page
        self._watch_load(page)
        self._observation = None
        self._handles = {}
        self._control_meta = {}

    def _finish_expected_navigation(self):
        expected = self._expected_location
        if expected is None:
            return
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            pages = list(self._context.pages)
            for page in pages:
                if page.url == expected:
                    self._page = page
                    self._expected_location = None
                    return
                # A just-opened popup briefly reports url "" before its first commit.
                if page.url not in ("", "about:blank") and self._origin(page.url) not in self.origins:
                    self._uncertain = True
                    raise EffectUncertain("browser_effect_outcome_uncertain")
            if pages:
                pages[-1].wait_for_timeout(50)
        self._uncertain = True
        raise EffectUncertain("browser_effect_outcome_uncertain")

    def observe(self) -> Observation:
        self._finish_expected_navigation()
        self._check()
        if self._page is None:
            raise RuntimeError("browser_not_started")
        self._handles = {}
        self._control_meta = {}
        controls = []
        pause_infos = []
        qualification_status = self._qualification_status() if getattr(self, "qualification_grant", None) is not None else None
        if self.qualification_grant is not None:
            self._page.wait_for_load_state('load',timeout=5000)
            for _ in range(20):
                if (self._load_pages.get(self._page) or {}).get('response') is not None:
                    break
                self._page.wait_for_timeout(25)
        if self.qualification_grant is not None and self.qualification_grant.frames:
            # A reviewed child document must finish before exporting its source facts.
            self._page.wait_for_load_state('load', timeout=5000)
            for _ in range(20):
                if all(record.get('response') is not None for record in self._qualification_frames.values()):
                    break
                self._page.wait_for_timeout(25)
        selector = (
            'a[href],button,[role="button"],summary,input,textarea,[role="checkbox"]'
            if self.general_mode else 'a[href],button,[role="button"],summary'
        )
        if qualification_status is not None:
            selector += ",select"
            if 'hover' in self.qualification_grant.allowed_operations:
                selector += ',[title],[tabindex]'
        reviewed_frames = self._qualification_reviewed_frames()
        self._qualification_observed_frames = [{"document_path":scope.document_path,
            "parent_document_path":scope.parent_document_path,"frame_id":scope.frame_id}
            for _, scope, _ in reviewed_frames]
        handles = _observed_handles(self._page, selector, 100)
        for frame, _, _ in reviewed_frames:
            if len(handles) < 100:
                handles.extend(_observed_handles(frame, selector, 100 - len(handles)))
        for handle in handles[:100]:
            if not handle.is_visible() or not handle.is_enabled():
                continue
            info = self._snapshot(handle)
            if not info["label"] and info["tag"] not in {"INPUT", "TEXTAREA"}:
                continue
            if info["href"]:
                try:
                    if self._origin(info["href"]) not in self.origins:
                        continue
                except Exception:
                    continue
                if qualification_status is not None and not self._qualification_link_allowed(info["href"], qualification_status):
                    continue
            pause = classify_control(info) if self.general_mode else (
                "send" if info.get("form") else None)
            qualified_field = (self._qualification_field(info, qualification_status, "type")
                or self._qualification_field(info, qualification_status, "click")) if qualification_status is not None else None
            qualified_submit = (any(rule.method == "POST" and rule.control_id == info.get("source_control_id")
                for rule in self._qualification_rules(info, qualification_status)) if qualification_status is not None else False)
            if (qualification_status is not None and info["tag"] == "BUTTON"
                    and info.get("source_container_id") and not qualified_submit):
                # A fixture container never turns an unreviewed/exhausted submit into a passive click.
                continue
            if qualification_status is not None and (qualified_field is not None or qualified_submit):
                pause = None
            if self.general_mode and pause:
                # Founder-satisfied controls no longer block the next pause.
                if pause == "password" and handle.evaluate("el => (el.value || '').trim()"):
                    pass
                elif pause == "otp_2fa" and handle.evaluate("el => (el.value || '').trim()"):
                    pass
                elif pause == "terms" and handle.evaluate("el => !!el.checked"):
                    pass
                else:
                    pause_infos.append(info)
            if not self.general_mode:
                if not info["label"] or info["form"]:
                    continue
                identifier = "c" + str(len(controls))
                self._handles[identifier] = (handle, info)
                self._control_meta[identifier] = {**info, "pause": None, "field_kind": "nav"}
                controls.append(Control(id=identifier, label=redact_string(info["label"] or "link"),
                                        operations=["click"], visible=True, enabled=True))
                continue
            # General mode: never offer pause targets or submit to Jev.
            if pause in {"password", "otp_2fa", "captcha", "terms", "payment", "send"}:
                continue
            operations = []
            if info["tag"] in {"A", "BUTTON", "SUMMARY"} or info.get("role") == "button":
                if not info.get("submit") or qualified_submit:
                    operations.append("click")
            if (qualification_status is not None and 'hover' in self.qualification_grant.allowed_operations
                    and info['tag'] in {'SPAN','DIV','TD','TR','LABEL'} and classify_control(info) is None):
                # Control's existing click slot is only the target carrier for this grant-only hover.
                info['source_hover_only'] = True
                operations.append('click')
            if qualified_field is not None and qualified_field.kind in {"checkbox", "radio"}:
                desired = qualified_field.value == "yes" if qualified_field.kind == "checkbox" else True
                if info.get("source_checked") is not desired:
                    operations.append("click")
            if qualified_field is not None and qualified_field.kind not in {"checkbox", "radio"}:
                desired = qualified_field.value if qualified_field.kind == "select" else qualified_field.input_value
                if info.get("source_value") != desired:
                    operations.append("type")
            if is_safe_type_target(info) and self.fill_values.get(field_kind(info)):
                # Skip fields that already hold a value so the loop can reach pauses.
                already = handle.evaluate("el => (el.value || '').trim()")
                if not already:
                    operations.append("type")
            if qualification_status is not None:
                grant = self.qualification_grant
                operations = [operation for operation in operations if operation in grant.allowed_operations
                    or operation == 'click' and info.get('source_hover_only') and 'hover' in grant.allowed_operations
                    or operation == "click" and info["tag"] == "A" and "navigate" in grant.allowed_operations
                    or operation == "type" and qualified_field is not None and qualified_field.kind == "select" and "select" in grant.allowed_operations
                    or operation == "click" and qualified_field is not None and qualified_field.kind in {"checkbox", "radio"} and "check" in grant.allowed_operations]
            operations = list(dict.fromkeys(operations))
            if not operations:
                continue
            identifier = "c" + str(len(controls))
            label = redact_string(info["label"] or info.get("name") or info.get("input_type") or "field")
            self._handles[identifier] = (handle, info)
            self._control_meta[identifier] = {
                **info, "pause": pause, "field_kind": field_kind(info),
            }
            controls.append(Control(id=identifier, label=label, operations=operations,
                                    visible=True, enabled=True))

        # Metadata and bounded visible text come from one current-document read.
        # The opaque document nonce is source-installed before page scripts.
        before = self._load_pages.get(self._page) or {}
        load_before = (before.get('sequence'), before.get('committed_sequence'), before.get('response'))
        visible = self._page.evaluate('''key => {
            let visibleText=false,omitted=false,count=0;
            const body=document.body;
            if(body){
                const walker=document.createTreeWalker(body,NodeFilter.SHOW_TEXT);
                const transparent=color=>color==='transparent'
                    ||/^rgba\\([^)]*,\\s*0(?:\\.0+)?\\s*\\)$/i.test(color)
                    ||/\\/\\s*0(?:\\.0+)?\\s*\\)$/i.test(color);
                for(let node=walker.nextNode();node;node=walker.nextNode()){
                    if(++count>2000){omitted=true;break;}
                    const parent=node.parentElement;
                    if(!parent || parent.closest('script,style,noscript,template,[hidden],[aria-hidden="true"]')
                        || !/[\\p{L}\\p{N}]/u.test(node.data.slice(0,4000))
                        || typeof parent.checkVisibility!=='function'
                        || !parent.checkVisibility({checkOpacity:true,checkVisibilityCSS:true}))continue;
                    const paint=getComputedStyle(parent);
                    if(transparent(paint.color)||transparent(paint.webkitTextFillColor))continue;
                    const range=document.createRange();range.selectNodeContents(node);
                    if(Array.from(range.getClientRects()).some(r=>{
                        if(r.width<=0||r.height<=0)return false;
                        let left=Math.max(0,r.left),right=Math.min(innerWidth,r.right),
                            top=Math.max(0,r.top),bottom=Math.min(innerHeight,r.bottom),depth=0;
                        for(let ancestor=parent;ancestor;ancestor=ancestor.parentElement){
                            if(++depth>32){omitted=true;return false;}
                            const style=getComputedStyle(ancestor),box=ancestor.getBoundingClientRect();
                            if(style.clipPath!=='none'||style.maskImage!=='none'||style.filter!=='none'
                                ||style.transform!=='none'){omitted=true;return false;}
                            const sx=ancestor.offsetWidth?box.width/ancestor.offsetWidth:1,
                                sy=ancestor.offsetHeight?box.height/ancestor.offsetHeight:1;
                            if(['hidden','clip','scroll','auto'].includes(style.overflowX)){
                                const edge=box.left+ancestor.clientLeft*sx;
                                left=Math.max(left,edge);right=Math.min(right,edge+ancestor.clientWidth*sx);
                            }
                            if(['hidden','clip','scroll','auto'].includes(style.overflowY)){
                                const edge=box.top+ancestor.clientTop*sy;
                                top=Math.max(top,edge);bottom=Math.min(bottom,edge+ancestor.clientHeight*sy);
                            }
                        }
                        // One whole visible text-line rectangle is enough for
                        // a document-load witness; a clipped sliver is unknown.
                        return right>left&&bottom>top&&left<=r.left+.5&&right>=r.right-.5
                            &&top<=r.top+.5&&bottom>=r.bottom-.5;
                    })){
                        visibleText=true;break;
                    }
                }
            }
            const cuaiSource = (CUAI_SOURCE)(CUAI_REVIEWED);
            const pageText=body?.innerText||'';
            const allBlocks=cuaiSource===null?[]:document.querySelectorAll('h1,h2,h3,p,li,tr,pre');
            const blocks=Array.from(allBlocks).slice(0,1000);
            if(allBlocks.length>1000)omitted=true;
            const clip=(text,limit)=>{
                const marker=' [truncated; omitted text is not verified]';
                return text.length>limit?text.slice(0,limit-marker.length)+marker:text;
            };
            const painted=el=>!el.closest('[hidden],[aria-hidden="true"]')
                &&el.checkVisibility?.({checkOpacity:true,checkVisibilityCSS:true});
            const viewport=[];
            for(const el of blocks){
                if(viewport.length>=20)break;
                const rectangle=el.getBoundingClientRect();
                if(painted(el)&&rectangle.bottom>0&&rectangle.top<innerHeight
                    &&rectangle.right>0&&rectangle.left<innerWidth)viewport.push(clip(el.innerText||'',300));
            }
            // ponytail: 12K-character task excerpts, use bounded document extract/scroll for exhaustive reads.
            let excerpt=pageText;
            if(pageText.length>12000){
                excerpt=pageText.slice(0,6000);
                const marker='\\n[truncated page text; omitted content is not verified]';
                const terms=new Set(CUAI_TERMS);
                for(const term of terms){
                    if(term.length>64)continue;
                    const index=pageText.search(new RegExp(term,'iu'));
                    if(index<6000)continue;
                    const passage='\\n[task excerpt] '+pageText.slice(Math.max(6000,index-160),index+term.length+240);
                    if(excerpt.length+passage.length+marker.length>12000)break;
                    excerpt+=passage;
                }
                excerpt+=marker;
            }
            return {text:pageText.slice(0,4000),page_text:cuaiSource===null?'':excerpt,title:clip(document.title,300),
                headings:blocks.filter(el=>/^H[1-3]$/.test(el.tagName)&&painted(el)).slice(0,64)
                    .map(el=>clip(el.innerText||'',160)),viewport:viewport.join('\\n'),visible_text:visibleText,
                source:cuaiSource,
                visibility_omitted:omitted,location:location.href,
                document_id:document[key]||null,ready_state:document.readyState};
        }'''.replace('CUAI_TERMS', json.dumps(list(dict.fromkeys(
                re.findall(r"[^\W_]{3,}", _cuai_redact(self.goal))))[:64]), 1)
            .replace('CUAI_REVIEWED', json.dumps(list(dict.fromkeys(self.fill_values.values())))
                     if getattr(self,'cuai_mode',False) else '[]', 1)
            .replace('CUAI_SOURCE', _CUAI_SOURCE_JS if getattr(self,'cuai_mode',False) else '() => null', 1), self._load_document_key)
        if reviewed_frames and isinstance(visible.get('source'), dict):
            for _, scope, source in reviewed_frames:
                for fact in source['facts']:
                    framed = 'Frame ' + scope.frame_id + ': ' + fact
                    if len(framed) <= 700:
                        visible['source']['facts'].append(framed)
        context = _cuai_redact(visible['text']) if getattr(self,'cuai_mode',False) else redact_string(visible['text'])
        pause_kind = page_requires_pause(pause_infos, context=context) if self.general_mode else None
        # Keep working on safe non-secret fields while a password/terms control is
        # merely present. Pause only when nothing safe remains for the agent.
        confirmation_pending = False
        # A real form or sensitive field takes precedence over unrelated
        # navigation once reviewed fields have been filled. A marketing page
        # can also contain a standalone "Sign up" button that *might* submit;
        # keep that button withheld, but do not freeze all safe same-origin
        # links before the agent has reached the actual form.
        if self.general_mode and pause_kind and not any("type" in c.operations for c in controls):
            safe_link = any(
                "click" in control.operations and self._control_meta.get(control.id, {}).get("tag") == "A"
                and any(phrase in control.label.casefold() for phrase in (
                    "sign up", "signup", "create account", "register", "get started", "start free"))
                for control in controls
            )
            standalone_send_with_navigation = (
                pause_kind == "send" and safe_link
                and all(not info.get("form") and not info.get("submit") for info in pause_infos)
            )
            readonly_send_presence = (self.read_only and self.qualification_grant is None and pause_kind == "send")
            # Submit controls stay withheld. Their presence cannot turn a
            # strictly read-only public document into permission to submit.
            confirmation_pending = not (standalone_send_with_navigation or readonly_send_presence)
        self.pause_kind = pause_kind if confirmation_pending else None
        self.founder_prompt = founder_prompt(pause_kind) if confirmation_pending and pause_kind else None
        if confirmation_pending:
            controls = []
            self._handles = {}
            self._control_meta = {}

        # Persist only bounded counts, never page labels or field values, so a
        # stopped native run can distinguish an empty observation from a model
        # choice of "blocked" despite a compatible signup link.
        self.last_control_summary = {
            "click": sum("click" in control.operations for control in controls),
            "type": sum("type" in control.operations for control in controls),
            "signup_links": sum(
                "click" in control.operations
                and self._control_meta.get(control.id, {}).get("tag") == "A"
                and any(phrase in control.label.casefold() for phrase in (
                    "sign up", "signup", "create account", "register", "get started", "start free"))
                for control in controls
            ),
        }

        if self.cuai_mode:
            from coding_agents.cu_ai_loop import CARD, SECRETISH
            page = "\n".join(["PAGE TITLE: " + _cuai_redact(visible.get('title', '')),
                "PAGE URL: " + _cuai_redact(self._page.url[:500]),
                *("HEADING: " + _cuai_redact(value) for value in visible.get('headings', [])),
                "VIEWPORT: " + _cuai_redact(visible.get('viewport', '')),
                "PAGE TEXT: " + _cuai_redact(visible.get('page_text', visible['text']))])
            if any(SECRETISH.search(value) or CARD.search(value)
                    for value in [page, *(control.label for control in controls)]):
                raise PermissionError('browser_cuai_sensitive_observation')
            context = bounded_page_text(page, self.goal, 4000)
            for control in controls:
                control.label = bounded_page_text(control.label, self.goal, 120)
        self._observation = Observation(
            revision=uuid4().hex, application="com.google.Chrome",
            location=self._page.url[:500], goal=self.goal, context=context,
            controls=controls, permitted=True,
            reviewed_text=(list(dict.fromkeys(self.resolve_fill(control.id) for control in controls if "type" in control.operations))
                if qualification_status is not None else list(dict.fromkeys(self.fill_values.values()))) if getattr(self,'cuai_mode',False) else [],
            confirmation_pending=confirmation_pending,
            prior_effect_uncertain=False)
        record = self._load_pages.get(self._page) or {}
        response = record.get('response')
        qualified = bool(isinstance(response, dict) and response.get('complete') is True
            and load_before == (record.get('sequence'), record.get('committed_sequence'), response)
            and type(response.get('status')) is int and 200 <= response['status'] < 300
            and response.get('status') not in {204, 205}
            and response.get('url') == visible.get('location') == self._page.url
            and record.get('sequence') == record.get('committed_sequence') == response.get('sequence')
            and isinstance(visible.get('document_id'), str) and visible.get('document_id')
            and record.get('document_id') in {None, visible.get('document_id')}
            and visible.get('ready_state') == 'complete' and visible.get('visible_text') is True)
        recoverable = (self.qualification_grant is not None and isinstance(response,dict)
            and response.get('complete') is True and 400 <= response.get('status',0) <= 599
            and response.get('request_id') and response.get('mime_type') == 'text/html'
            and response.get('url') == visible.get('location') == self._page.url
            and load_before == (record.get('sequence'),record.get('committed_sequence'),response)
            and record.get('sequence') == record.get('committed_sequence') == response.get('sequence')
            and visible.get('document_id') and record.get('document_id') in {None,visible['document_id']}
            and visible.get('ready_state') == 'complete' and visible.get('visible_text') is True)
        if qualified or recoverable:
            record['document_id'] = visible['document_id']
        if recoverable:
            self._observation.controls = []
            self._observation.reviewed_text = []
            self._observation.confirmation_pending = False
            self.pause_kind = self.founder_prompt = None
            self._handles, self._control_meta = {}, {}
        self.last_load_evidence = {'qualified': qualified, 'session_id': self.session_id,
            'page_id': record.get('page_id'),
            'sequence': record.get('sequence'), 'response': response,
            'location': visible.get('location'), 'document_id': visible.get('document_id'),
            'ready_state': visible.get('ready_state'), 'redacted_body_text_sha256': hashlib.sha256(context.encode()).hexdigest(),
            'visible_text': visible.get('visible_text') is True,
            'visibility_omitted': visible.get('visibility_omitted') is True,
            'visibility_scope': 'bounded_dom_text_geometry',
            'observation_digest': self._observation.digest()}
        if recoverable:
            self.last_load_evidence['kind'] = 'qualification_error'
        if getattr(self,'cuai_mode',False):
            self._cuai_current = None
            if qualified or recoverable:
                entry = {'session_id':self.session_id,'page_id':record['page_id'],
                    'document_id':visible['document_id'],'sequence':record['sequence'],
                    'location':self._page.url,'observation_digest':self._observation.digest(),
                    'source':_cuai_source(visible.get('source'))}
                if (getattr(self, "cuai_version", "browser_cuai_text_v1") == "browser_cuai_text_v2"
                        or getattr(self,'bounded_reads',False) or getattr(self, "qualification_grant", None) is not None):
                    if self._cuai_read_document != visible['document_id']:
                        self._cuai_reads, self._cuai_reads_omitted = [], 0
                        self._cuai_read_document = visible['document_id']
                    source = entry['source']
                    source.update(reads=list(self._cuai_reads), reads_omitted=self._cuai_reads_omitted)
                    while len(json.dumps(source, ensure_ascii=False, separators=(',', ':')).encode()) > 3000:
                        # Whole targeted reads outrank generic page prose.
                        if source['facts']:
                            source['facts'].pop()
                            source['omitted']['facts'] += 1
                        elif source['fields']:
                            source['fields'].pop()
                            source['omitted']['fields'] += 1
                        elif source['reads']:
                            source['reads'].pop(0)
                            source['reads_omitted'] += 1
                        else:
                            break
                self._cuai_current = entry
                if not self._cuai_pages or self._cuai_pages[-1]['source'] != entry['source'] or self._cuai_pages[-1]['document_id'] != entry['document_id']:
                    self._cuai_pages.append(entry)
                    if len(self._cuai_pages)>8:
                        self._cuai_pages.pop(0)
                        self._cuai_omitted['pages'] += 1
        return self._observation

    def cuai_readback(self, observation: Observation, limit=5000) -> str:
        """Current source plus whole historical inputs; no added browser reads."""
        self._check()
        if (not getattr(self,'cuai_mode',False) or not (self.verified_load(observation) or self.qualification_recoverable_load(observation))
                or self._cuai_current is None):
            raise PermissionError('browser_cuai_evidence_unavailable')
        current = self._cuai_current
        if current['observation_digest'] != observation.digest() or current['session_id'] != self.session_id:
            raise PermissionError('browser_cuai_evidence_stale')
        pages = [entry for entry in self._cuai_pages if entry is not current]
        inputs = list(self._cuai_inputs)
        omitted = dict(self._cuai_omitted)
        result={'schema_version':2 if (getattr(self, "cuai_version", "browser_cuai_text_v1") == "browser_cuai_text_v2"
                or getattr(self,'bounded_reads',False) or getattr(self, "qualification_grant", None) is not None) else 1,'scope':'source_main_document_inputs_not_acceptance',
                'current':current,'prior_pages':pages,'input_history':inputs,'omitted':omitted}
        if self.last_qualification_download is not None:
            result['download'] = dict(self.last_qualification_download)
        if self.last_qualification_read_response is not None:
            result['failed_download'] = dict(self.last_qualification_read_response)
        def size():return len(json.dumps(result,ensure_ascii=False,separators=(',',':')).encode())
        while size()>limit and (pages or inputs):
            if pages:
                pages.pop(0)
                omitted['pages'] += 1
            else:
                inputs.pop(0)
                omitted['inputs'] += 1
        if size()>limit:
            raise PermissionError('browser_cuai_evidence_over_bound')
        return json.dumps(result,ensure_ascii=False,separators=(',',':'))

    def cuai_navigation_targets(self, observation):
        self._check()
        if ((getattr(self, "cuai_version", "browser_cuai_text_v1") != "browser_cuai_text_v2"
                and not getattr(self,'bounded_reads',False) and getattr(self, "qualification_grant", None) is None)
                or not (self.verified_load(observation) or self.qualification_recoverable_load(observation)) or observation is not self._observation):
            raise PermissionError('browser_cuai_evidence_stale')
        grant = getattr(self, "qualification_grant", None)
        if grant is not None and "navigate" not in grant.allowed_operations:
            return []
        status = self._qualification_status() if grant is not None else None
        links = []
        for control in observation.controls:
            info = self._control_meta.get(control.id) or {}
            href = info.get('href')
            if (not control.visible or not control.enabled or 'click' not in control.operations
                    or info.get('tag') != 'A' or info.get('form') or classify_control(info) is not None
                    or not isinstance(href, str) or not 0 < len(href) <= 500 or redact_string(href) != href
                    or _cuai_redact(control.label) != control.label):
                continue
            try:
                if self._origin(href) != self._origin(observation.location):
                    continue
            except (ValueError, PermissionError):
                continue
            if status is not None and not self._qualification_link_allowed(href, status):
                continue
            privacy_href = href
            if grant is not None:
                prefix = grant.origin + '/public/browser-qualification/' + grant.nonce + '/'
                if href.startswith(prefix):
                    # Only an exact current-grant GET nonce is custody metadata.
                    privacy_href = grant.origin + '/public/browser-qualification/owned/' + href[len(prefix):]
            if _cuai_redact(privacy_href) != privacy_href:
                continue
            links.append({'id':control.id,'href':href,'tag':'A','form':False})
            if len(links) == 16:
                break
        return links

    def bound_cuai_observation(self, observation, page_bytes):
        self._check()
        if (observation is not self._observation
                or not (self.verified_load(observation) or self.qualification_recoverable_load(observation))):
            raise PermissionError('browser_cuai_evidence_stale')
        projected = bounded_page_observation(observation, page_bytes)
        observation.context, observation.controls = projected.context, projected.controls
        digest = observation.digest()
        self.last_load_evidence['observation_digest'] = digest
        self.last_load_evidence['redacted_body_text_sha256'] = hashlib.sha256(observation.context.encode()).hexdigest()
        if self._cuai_current is not None:
            self._cuai_current['observation_digest'] = digest

    def _cuai_input(self, before, invocation_id, operation, status):
        if before is None:
            return
        self._cuai_inputs.append({**before,'invocation_id':invocation_id,'operation':operation,
                                  'delivery':status,'acceptance':'unknown'})
        if len(self._cuai_inputs)>8:
            self._cuai_inputs.pop(0)
            self._cuai_omitted['inputs'] += 1

    def resolve_fill(self, target: str | None) -> str | None:
        """Return only the reviewed value for the freshly observed field."""
        if not self.general_mode or not target or target not in self._handles:
            return None
        meta = self._control_meta.get(target) or {}
        if getattr(self, "qualification_grant", None) is not None:
            field = self._qualification_field(meta, self._qualification_status(), "type")
            return (field.value if field.kind == "select" else field.input_value) if field is not None else None
        kind = meta.get("field_kind")
        if kind not in {"email", "name", "text", "tel"}:
            return None
        return self.fill_values.get(kind)

    def _input_binding(self, handle):
        """Retain source identities, never resolve a replacement by its label."""
        page = self._page
        record = getattr(self, '_load_pages', {}).get(page) or {}
        document = record.get('document_id')
        key = getattr(self, '_load_document_key', None)
        if key is not None:
            try:
                identity = handle.evaluate(
                    'el=>({connected:el.isConnected,document:el.ownerDocument['
                    + json.dumps(key) + ']||null})')
                if identity.get('connected') is not True or not identity.get('document'):
                    raise ValueError('unbound document')
                document = identity['document']
            except Exception:
                raise BrowserPreDispatchRefused(
                    'browser_pre_dispatch_fresh_observation_required') from None
        return (page, page.url, record.get('page_id'), record.get('sequence'),
                record.get('committed_sequence'), document)

    def _recheck_before_input(self, handle, before, binding, *, empty=False):
        """Check the admission interval before ANY native input call.

        A later DOM change during Playwright input remains an uncertain effect;
        this remote read and the subsequent input are not an atomic operation.
        """
        try:
            self._check()
        except Exception:
            raise BrowserPreDispatchRefused('browser_pre_dispatch_scope_retired') from None
        try:
            fresh = self._input_binding(handle)
            current = self._snapshot(handle)
            if (fresh != binding or current != before or not handle.is_visible()
                    or not handle.is_enabled() or empty and current.get('has_value') is not False):
                raise BrowserPreDispatchRefused('browser_pre_dispatch_target_changed')
        except BrowserPreDispatchRefused:
            raise
        except Exception:
            raise BrowserPreDispatchRefused(
                'browser_pre_dispatch_fresh_observation_required') from None

    def click(self, candidate: Candidate, invocation_id: str, *, navigate_url=None, new_tab_url=None):
        if getattr(self, "read_only", False) and navigate_url is None:
            raise PermissionError("browser_effect_requires_review")
        self._check()
        if navigate_url is not None and new_tab_url is not None:
            raise PermissionError("browser_candidate_invalid")
        operation = 'new_tab' if new_tab_url is not None else "navigate" if navigate_url is not None else "click"
        if self._observation is None or candidate.operation != operation or candidate.target not in self._handles:
            raise PermissionError("browser_candidate_invalid")
        verify_current(Candidate("click", candidate.target, candidate.observation_digest)
                       if navigate_url is not None or new_tab_url is not None else candidate, self._observation)
        handle, original = self._handles[candidate.target]
        current = self._snapshot(handle)
        if original.get('source_hover_only'):
            raise PermissionError('browser_candidate_invalid')
        if new_tab_url is not None and (self.qualification_grant is None
                or 'new_tab' not in self.qualification_grant.allowed_operations
                or len([page for page in self._qualification_owned_pages if not page.is_closed()]) >= self.qualification_grant.max_tabs
                or {'id':candidate.target,'href':new_tab_url,'tag':'A','form':False} not in self.cuai_navigation_targets(self._observation)
                or current.get('href') != new_tab_url or current.get('tag') != 'A' or current.get('form')):
            raise PermissionError('browser_candidate_invalid')
        if navigate_url is not None and (not self._read_op_allowed("navigate")
                or {'id':candidate.target,'href':navigate_url,'tag':'A','form':False} not in self.cuai_navigation_targets(self._observation)
                or current.get('href') != navigate_url or current.get('tag') != 'A' or current.get('form')):
            raise PermissionError("browser_candidate_invalid")
        if (current.get("label") != original.get("label") or current.get("href") != original.get("href")
                or not handle.is_visible() or not handle.is_enabled()
                or self._page.url != self._observation.location):
            raise PermissionError("browser_observation_stale")
        qualified_rules = []
        if getattr(self, "qualification_grant", None) is not None and operation == 'click':
            status = self._qualification_status()
            if any(row["protected"] is True for row in status["rules"].values()):
                raise EffectUncertain("browser_qualification_request_unconfirmed")
            proposal = self._qualification_proposal
            if proposal is None or proposal[:2] != (invocation_id, candidate.observation_digest):
                raise PermissionError("browser_qualification_proposal_invalid")
            qualified_rules = [rule for rule in self._qualification_rules(current, status)
                if rule.method == "POST" and rule.control_id == current.get("source_control_id") and rule.rule_id in proposal[2]]
            qualified_check = self._qualification_field(current, status, "click") is not None
            if "click" not in self.qualification_grant.allowed_operations and not qualified_check:
                raise PermissionError("browser_qualification_operation_unavailable")
        else:
            qualified_check = False
        download = None
        if getattr(self,'qualification_grant',None) is not None and current.get('href'):
            status = self._qualification_status()
            download = self._qualification_download_rule(current['href'], status)
            if download is not None and (getattr(self,'read_only',False) or new_tab_url is not None or self.last_qualification_download is not None):
                raise PermissionError('browser_qualification_download_unreviewed')
        popup = (getattr(self,'qualification_grant',None) is not None and operation == 'click'
            and current.get('href') and handle.evaluate("el => el.target === '_blank'"))
        if popup and ('new_tab' not in self.qualification_grant.allowed_operations
                or len([page for page in self._qualification_owned_pages if not page.is_closed()]) >= self.qualification_grant.max_tabs):
            raise PermissionError('browser_qualification_tab_unreviewed')
        if (getattr(self, "general_mode", False) and classify_control(current) is not None
                and not qualified_rules and not qualified_check):
            raise PermissionError("browser_effect_requires_founder")
        if not getattr(self, "general_mode", False) and (current.get("form") or current.get("tag") not in {"A", "SUMMARY"}):
            raise PermissionError("browser_effect_requires_review")
        binding = self._input_binding(handle)
        effect = {"session": self.session_id, "order": self.order_id, "invocation": invocation_id,
                  "observation": candidate.observation_digest, "operation": operation,
                  "control": {"tag": current["tag"], "label": redact_string(current.get("label") or ""),
                              "form": bool(current.get("form")), "href": current.get("href")},
                  "location": self._page.url}
        if getattr(self, "qualification_grant", None) is not None:
            effect["control"].update({key: current[key] for key in ("source_control_id", "source_document_id", "source_document_path", "source_container_id")})
            effect["control"]["submit"] = current.get("submit") is True
        if qualified_rules:
            effect["qualification_rule_ids"] = [rule.rule_id for rule in qualified_rules]
            effect["control"].update({key: current[key] for key in ("source_control_id", "source_document_id", "source_document_path", "source_container_id")})
        if download is not None:
            effect['qualification_download_rule_id'] = download.rule_id
        digest = hashlib.sha256(json.dumps(effect, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if self.admit_effect(effect, digest) is not True:
            raise PermissionError("browser_effect_not_admitted")
        self._recheck_before_input(handle, current, binding)
        if qualified_rules:
            self._qualification_pending = {"invocation": invocation_id, "effect_digest": digest,
                "rule_ids": tuple(rule.rule_id for rule in qualified_rules), "handle": handle,
                "binding": binding, "locked": False}
        if download is not None:
            self._qualification_download_pending = {'page':self._page, 'url':current['href'], 'contract':download,
                'invocation':invocation_id, 'effect_digest':digest, 'observation_digest':candidate.observation_digest,
                'source_url':self._page.url,'load_before':dict(self._load_pages[self._page])}
        if new_tab_url is not None or popup:
            self._qualification_opening = {'url':new_tab_url or current['href'], 'page':None}
        before = self._cuai_current if getattr(self,'cuai_mode',False) else None
        self._observation = None
        try:
            if new_tab_url is not None or popup:
                # Create the admitted owned tab directly: a native popup's first request
                # has no Frame custody yet and must remain blocked by the route gate.
                page = self._context.new_page()
                self._expected_location = new_tab_url or current['href']
                page.goto(self._expected_location, wait_until='domcontentloaded', timeout=10000)
                self._page = page
            elif navigate_url is not None:
                self._expected_location = None if download is not None else navigate_url
                try:
                    self._page.goto(navigate_url, wait_until="domcontentloaded", timeout=10000)
                except Exception as error:
                    if download is None or not any(message in str(error) for message in ('net::ERR_ABORTED','Download is starting')):
                        raise
            elif current.get("href"):
                self._expected_location = None if download is not None else current["href"]
                handle.click(timeout=5000, no_wait_after=True)
            else:
                handle.click(timeout=5000)
            self._qualification_finish()
            if getattr(self,'_qualification_redirect',None) is not None:
                destination = self._qualification_redirect
                self._qualification_redirect = None
                self._page.goto(destination,wait_until='domcontentloaded',timeout=10000)
            if download is not None:
                for _ in range(20):
                    if self._qualification_download_finish():
                        break
                    self._page.wait_for_timeout(100)
                else:
                    raise ValueError('download_delivery_unconfirmed')
            if getattr(self,'_qualification_opening',None) is not None:
                self._finish_expected_navigation()
                if self._qualification_opening['page'] is not self._page:
                    raise ValueError('tab_delivery_unconfirmed')
                self._qualification_opening = None
            self._check()
            self._cuai_input(before,invocation_id,operation,'input_delivered')
        except Exception:
            self._cuai_input(before,invocation_id,operation,'delivery_unconfirmed')
            self._uncertain = True
            raise EffectUncertain("browser_effect_outcome_uncertain") from None

    def hover(self, candidate, invocation_id):
        if getattr(self,'read_only',False):
            raise PermissionError('browser_effect_requires_review')
        self._check()
        observation = self._observation
        if (self.qualification_grant is None or 'hover' not in self.qualification_grant.allowed_operations
                or observation is None or candidate.operation != 'hover' or candidate.target not in self._handles
                or candidate.value_index is not None or candidate.requires_outcome_verification):
            raise PermissionError('browser_candidate_invalid')
        verify_current(Candidate('click', candidate.target, candidate.observation_digest), observation)
        handle, original = self._handles[candidate.target]
        current, binding = self._snapshot(handle), self._input_binding(handle)
        if (classify_control(current) is not None or current.get('form') or current.get('submit')
                or current.get('tag') in {'INPUT','TEXTAREA','SELECT'} or not handle.is_visible()
                or current.get('label') != original.get('label') or current.get('href') != original.get('href')):
            raise PermissionError('browser_candidate_invalid')
        effect = {'session':self.session_id, 'order':self.order_id, 'invocation':invocation_id,
            'observation':candidate.observation_digest, 'operation':'hover', 'location':observation.location,
            'control':{'tag':current['tag'], 'label':redact_string(current.get('label') or ''),
                'form':False, 'href':current.get('href'), **{key:current[key] for key in (
                    'source_control_id','source_document_id','source_document_path','source_container_id')}}}
        digest = hashlib.sha256(json.dumps(effect,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        if self.admit_effect(effect, digest) is not True:
            raise PermissionError('browser_effect_not_admitted')
        self._recheck_before_input(handle,current,binding)
        before = self._cuai_current
        self._observation = None
        try:
            handle.hover(timeout=5000)
            self._check()
            self._cuai_input(before,invocation_id,'hover','input_delivered')
        except Exception:
            self._uncertain = True
            self._cuai_input(before,invocation_id,'hover','delivery_unconfirmed')
            raise EffectUncertain('browser_effect_outcome_uncertain') from None

    def tab_action(self, candidate, invocation_id, action):
        if getattr(self,'read_only',False):
            raise PermissionError('browser_effect_requires_review')
        self._check()
        observation = self._observation
        if (self.qualification_grant is None or action.op not in {'switch_tab','new_tab'}
                or action.op not in self.qualification_grant.allowed_operations or observation is None
                or candidate.operation != action.op or candidate.observation_digest != observation.digest()
                or candidate.value_index is not None or candidate.requires_outcome_verification
                or not self.verified_load(observation)):
            raise PermissionError('browser_candidate_invalid')
        if action.op == 'new_tab':
            status = self._qualification_status()
            if (candidate.target != cuai_document_target(self.last_load_evidence,action)
                    or not self._qualification_link_allowed(action.text,status)
                    or self._qualification_download_rule(action.text,status) is not None
                    or len([item for item in self._qualification_owned_pages if not item.is_closed()]) >= self.qualification_grant.max_tabs):
                raise PermissionError('browser_candidate_invalid')
            handle = self._page.locator('html').element_handle()
            current, binding = self._snapshot(handle), self._input_binding(handle)
            effect = {'session':self.session_id,'order':self.order_id,'invocation':invocation_id,
                'observation':candidate.observation_digest,'operation':action.op,'location':observation.location,
                'document_target':candidate.target,'tab_url':action.text,
                'control':{'tag':'HTML','label':'Reviewed owned tab','form':False}}
            digest = hashlib.sha256(json.dumps(effect,sort_keys=True,separators=(',',':')).encode()).hexdigest()
            if self.admit_effect(effect,digest) is not True:
                raise PermissionError('browser_effect_not_admitted')
            self._recheck_before_input(handle,current,binding)
            before = self._cuai_current
            self._observation = None
            self._qualification_opening = {'url':action.text,'page':None}
            try:
                page = self._context.new_page()
                page.goto(action.text,wait_until='domcontentloaded',timeout=10000)
                if self._qualification_opening['page'] is not page or page.url != action.text:
                    raise ValueError('tab_delivery_unconfirmed')
                self._page = page
                self._qualification_opening = None
                self._check()
                self._cuai_input(before,invocation_id,action.op,'input_delivered')
                return
            except Exception:
                self._uncertain = True
                self._cuai_input(before,invocation_id,action.op,'delivery_unconfirmed')
                raise EffectUncertain('browser_effect_outcome_uncertain') from None
        if candidate.target != action.element_id:
            raise PermissionError('browser_candidate_invalid')
        tabs = self._qualification_tabs(self._qualification_status())
        offered = next((item for item in tabs if item['id'] == candidate.target), None)
        page = next((item for item in self._qualification_owned_pages
            if 't' + self._load_pages[item]['page_id'] == candidate.target and not item.is_closed()), None)
        if offered is None or page is None or page is self._page or page.url != offered['url']:
            raise PermissionError('browser_candidate_invalid')
        handle = self._page.locator('html').element_handle()
        current, binding = self._snapshot(handle), self._input_binding(handle)
        target_record = self._load_pages[page]
        target_binding = (page.url, target_record['sequence'], target_record['committed_sequence'], target_record['document_id'])
        effect = {'session':self.session_id, 'order':self.order_id, 'invocation':invocation_id,
            'observation':candidate.observation_digest, 'operation':action.op, 'location':observation.location,
            'tab_target':candidate.target, 'tab_url':offered['url'],
            'control':{'tag':'HTML','label':'Reviewed owned tab','form':False}}
        digest = hashlib.sha256(json.dumps(effect,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        if self.admit_effect(effect,digest) is not True:
            raise PermissionError('browser_effect_not_admitted')
        self._recheck_before_input(handle,current,binding)
        if (page.is_closed() or target_binding != (page.url, target_record['sequence'],
                target_record['committed_sequence'],target_record['document_id'])):
            raise BrowserPreDispatchRefused('browser_pre_dispatch_target_changed')
        before = self._cuai_current
        self._observation = None
        self._page = page
        self._cuai_input(before,invocation_id,action.op,'input_delivered')

    def _read_op_allowed(self, op):
        """v2 reads/navigation, or exactly the operations a controller fixture grant names."""
        grant = getattr(self, "qualification_grant", None)
        if grant is not None:
            return op in grant.allowed_operations
        return op in {'navigate','extract','scroll'} and (getattr(self, "cuai_version", "browser_cuai_text_v1") == "browser_cuai_text_v2"
            or getattr(self,'bounded_reads',False))

    def document_action(self, candidate, invocation_id, action):
        """One source-admitted read/scroll over the exact observed document."""
        if action.op == 'navigate' and self.qualification_grant is not None:
            return self._qualification_recovery_navigation(candidate,invocation_id,action)
        self._check()
        observation = self._observation
        if (not self._read_op_allowed(action.op) or observation is None
                or not self.verified_load(observation) or not observation.permitted or observation.confirmation_pending
                or observation.prior_effect_uncertain or candidate.operation != action.op
                or candidate.target != cuai_document_target(self.last_load_evidence, action)
                or action.op not in {"extract", "scroll", "navigate"}
                or action.op == 'navigate' and (not self.read_only or action.text not in self.read_urls)):
            raise PermissionError("browser_candidate_invalid")
        if (candidate.observation_digest != observation.digest() or candidate.value_index is not None
                or candidate.requires_outcome_verification):
            raise PermissionError("browser_candidate_invalid")
        if action.op == "scroll" and (action.direction not in {"up","down","left","right"}
                or type(action.amount) is not int or not 1 <= action.amount <= 5):
            raise PermissionError("browser_candidate_invalid")
        if action.op == "extract" and (not 0 < len(action.text.strip()) <= 160 or _cuai_redact(action.text) != action.text):
            raise PermissionError("browser_candidate_invalid")
        handle = self._page.locator('html').element_handle()
        if handle is None:
            raise BrowserPreDispatchRefused('browser_pre_dispatch_fresh_observation_required')
        current, binding = self._snapshot(handle), self._input_binding(handle)
        effect = {'session':self.session_id,'order':self.order_id,'invocation':invocation_id,
                  'observation':candidate.observation_digest,'operation':action.op,
                  'document_target':candidate.target,'location':observation.location,
                  'control':{'tag':'HTML','form':False,'label':'Reviewed document'}}
        if action.op == 'navigate':
            effect['read_url'] = action.text
        digest = hashlib.sha256(json.dumps(effect,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        if self.admit_effect(effect, digest) is not True:
            raise PermissionError('browser_effect_not_admitted')
        self._recheck_before_input(handle, current, binding)
        before = self._cuai_current
        self._observation = None
        try:
            if action.op == 'navigate':
                self._reviewed_read_navigation = action.text
                try:
                    self._page.goto(action.text, wait_until='domcontentloaded', timeout=10000)
                finally:
                    self._reviewed_read_navigation = None
                if self._page.url != action.text:
                    raise ValueError('reviewed_read_navigation_changed')
            elif action.op == 'scroll':
                delta = 200 * action.amount
                dx = (-delta if action.direction=='left' else delta) if action.direction in {'left','right'} else 0
                dy = (-delta if action.direction=='up' else delta) if action.direction in {'up','down'} else 0
                handle.evaluate('(el, delta) => el.ownerDocument.defaultView.scrollBy(delta[0],delta[1])', [dx,dy])
            else:
                raw = handle.evaluate('(el,query)=>(' + _CUAI_EXTRACT_JS + ')(query)', action.text)
                if self._input_binding(handle) != binding:
                    raise ValueError('source_changed_during_read')
                read = {'query':action.text,'passages':[_cuai_redact(text) for text in raw['passages']], 'omitted':raw['omitted'],
                        'invocation_id':invocation_id,'observation_digest':candidate.observation_digest,'historical':True}
                while len(json.dumps(read,ensure_ascii=False,separators=(',',':')).encode()) > 1600 and read['passages']:
                    read['passages'].pop()
                    read['omitted'] += 1
                self._cuai_reads.append(read)
                if len(self._cuai_reads) > 4:
                    self._cuai_reads.pop(0)
                    self._cuai_reads_omitted += 1
            self._check()
            self._cuai_input(before,invocation_id,action.op,'input_delivered')
        except Exception:
            self._cuai_input(before,invocation_id,action.op,'delivery_unconfirmed')
            self._uncertain = True
            raise EffectUncertain("browser_effect_outcome_uncertain") from None

    def _qualification_recovery_navigation(self,candidate,invocation_id,action):
        self._check()
        observation = self._observation
        if (observation is None or not self.qualification_recoverable_load(observation)
                or 'navigate' not in self.qualification_grant.allowed_operations
                or candidate.operation != 'navigate' or candidate.observation_digest != observation.digest()
                or candidate.target != cuai_document_target(self.last_load_evidence,action)
                or candidate.value_index is not None or candidate.requires_outcome_verification):
            raise PermissionError('browser_candidate_invalid')
        status = self._qualification_status()
        if not self._qualification_link_allowed(action.text,status):
            raise PermissionError('browser_candidate_invalid')
        if getattr(self,'read_only',False) and self._qualification_download_rule(action.text,status) is not None:
            raise PermissionError('browser_effect_requires_review')
        handle = self._page.locator('html').element_handle()
        current,binding = self._snapshot(handle),self._input_binding(handle)
        effect = {'session':self.session_id,'order':self.order_id,'invocation':invocation_id,
            'observation':candidate.observation_digest,'operation':'navigate','location':observation.location,
            'document_target':candidate.target,'recovery_url':action.text,
            'control':{'tag':'HTML','label':'Reviewed error recovery','form':False}}
        digest = hashlib.sha256(json.dumps(effect,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        if self.admit_effect(effect,digest) is not True:
            raise PermissionError('browser_effect_not_admitted')
        self._recheck_before_input(handle,current,binding)
        download = self._qualification_download_rule(action.text,status)
        if download is not None:
            self._qualification_download_pending = {'page':self._page,'url':action.text,'contract':download,
                'invocation':invocation_id,'effect_digest':digest,'observation_digest':candidate.observation_digest,
                'source_url':self._page.url,'load_before':dict(self._load_pages[self._page])}
        before = self._cuai_current
        self._observation = None
        try:
            try:
                self._page.goto(action.text,wait_until='domcontentloaded',timeout=10000)
            except Exception as error:
                if download is None or not any(message in str(error) for message in ('net::ERR_ABORTED','Download is starting')):
                    raise
            if download is not None:
                for _ in range(20):
                    if self._qualification_download_finish():
                        break
                    self._page.wait_for_timeout(100)
                else:
                    raise ValueError('download_delivery_unconfirmed')
            self._check()
            self._cuai_input(before,invocation_id,'navigate','input_delivered')
        except Exception:
            self._uncertain = True
            self._cuai_input(before,invocation_id,'navigate','delivery_unconfirmed')
            raise EffectUncertain('browser_effect_outcome_uncertain') from None

    def type_text(self, candidate: Candidate, invocation_id: str, text: str):
        """Type a reviewed fill value. Receipts record the label only, never text."""
        if getattr(self, "read_only", False):
            raise PermissionError("browser_effect_requires_review")
        self._check()
        if (not self.general_mode or self._observation is None or candidate.operation != "type"
                or candidate.target not in self._handles or not isinstance(text, str)
                or not 0 < len(text) <= 320):
            raise PermissionError("browser_type_invalid")
        verify_current(candidate, self._observation)
        handle, original = self._handles[candidate.target]
        current = self._snapshot(handle)
        meta = self._control_meta.get(candidate.target) or {}
        field = (self._qualification_field(current, self._qualification_status(), "type")
            if getattr(self, "qualification_grant", None) is not None else None)
        if getattr(self, "qualification_grant", None) is not None and (field is None
                or text != (field.value if field.kind == "select" else field.input_value)):
            raise PermissionError("browser_qualification_fill_unreviewed")
        if ((not is_safe_type_target(current) and field is None) or field_kind(current) != meta.get("field_kind")
                or current.get('label') != original.get('label')
                or (getattr(self, 'cuai_mode', False) and field is None and current.get('has_value') is not False)
                or not handle.is_visible() or not handle.is_enabled()
                or self._page.url != self._observation.location):
            raise PermissionError("browser_observation_stale")
        binding = self._input_binding(handle)
        label = redact_string(current.get("label") or meta.get("field_kind") or "field")
        effect = {"session": self.session_id, "order": self.order_id, "invocation": invocation_id,
                  "observation": candidate.observation_digest, "operation": "type",
                  "control": {"tag": current["tag"], "label": label, "typed": True,
                              "field_kind": meta.get("field_kind")},
                  "location": self._page.url}
        digest = hashlib.sha256(json.dumps(effect, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if self.admit_effect(effect, digest) is not True:
            raise PermissionError("browser_effect_not_admitted")
        self._recheck_before_input(handle, current, binding, empty=getattr(self, 'cuai_mode', False) and field is None)
        before = self._cuai_current if getattr(self,'cuai_mode',False) else None
        self._observation = None
        self.last_typed_label = label
        try:
            if field is not None and field.kind == "select":
                handle.select_option(value=text, timeout=5000)
            else:
                handle.fill("")
                handle.type(text, delay=20)
            self._check()
            self._cuai_input(before,invocation_id,'type','input_delivered')
        except Exception:
            self._cuai_input(before,invocation_id,'type','delivery_unconfirmed')
            self._uncertain = True
            raise EffectUncertain("browser_effect_outcome_uncertain") from None

    def fill_reviewed_fields(self) -> list[dict]:
        """Fill reviewed non-secret fields by role without asking Jev.

        Each field uses the same one-use admit_effect path as type_text. Returns
        receipt-safe records (label + typed) and never includes values.
        """
        if getattr(self, "read_only", False):
            raise PermissionError("browser_effect_requires_review")
        if not self.general_mode or not self.fill_values:
            return []
        self._check()
        filled = []
        mapping = [
            ("email", 'input[type="email"], input[autocomplete="email"], input[name="email"]'),
            ("name", 'input[autocomplete="name"], input[name="name"]'),
            ("tel", 'input[type="tel"], input[autocomplete="tel"]'),
            ("text", 'input[type="text"]'),
        ]
        for kind, selector in mapping:
            value = self.fill_values.get(kind)
            if not value:
                continue
            locator = self._page.locator(selector)
            count = min(locator.count(), 5)
            for index in range(count):
                handle = locator.nth(index).element_handle()
                if handle is None or not handle.is_visible() or not handle.is_enabled():
                    continue
                info = self._snapshot(handle)
                if info.get("input_type") == "password" or not is_safe_type_target(info):
                    continue
                kind_now = field_kind(info)
                if kind == "email" and kind_now != "email":
                    continue
                if kind == "name" and kind_now not in {"name", "text"}:
                    continue
                if kind == "text" and kind_now in {"email", "name", "tel", "password"}:
                    continue
                already = handle.evaluate("el => (el.value || '').trim()")
                if already:
                    continue
                binding = self._input_binding(handle)
                label = redact_string(info.get("label") or kind)
                invocation = "din_fill_" + uuid4().hex
                observation = hashlib.sha256(
                    f"{self._page.url}|{label}|{kind}".encode()).hexdigest()
                effect = {
                    "session": self.session_id, "order": self.order_id,
                    "invocation": invocation, "observation": observation,
                    "operation": "type",
                    "control": {
                        "tag": info.get("tag") or "INPUT", "label": label,
                        "typed": True, "field_kind": kind,
                    },
                    "location": self._page.url,
                }
                digest = hashlib.sha256(
                    json.dumps(effect, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                if self.admit_effect(effect, digest) is not True:
                    return filled
                self._recheck_before_input(handle, info, binding, empty=True)
                try:
                    handle.fill("")
                    handle.type(value, delay=10)
                    self._check()
                except Exception:
                    self._uncertain = True
                    raise EffectUncertain("browser_effect_outcome_uncertain") from None
                filled.append({"kind": "effect", "operation": "type", "label": label, "typed": True,
                               "field_kind": kind})
                self.last_typed_label = label
                break
        return filled

    def _stop_factory(self):
        if self._factory is not None:
            self._factory.stop()  # kills the Chrome process group; the persistent profile stays on disk

    def close(self):
        try:
            if self._browser:
                self._browser.close()
        finally:
            try:
                if self._playwright:
                    self._playwright.stop()
            finally:
                self._close_state()

    def _close_state(self):
        try:
            self._page = self._context = self._browser = self._playwright = None
            self._observation = None
            self._handles = {}
            self._control_meta = {}
            self._expected_location = None
            self._load_pages = {}
            self._load_unbound_requests = set()
            self.last_load_evidence = None
            self._cuai_current = None
            self._cuai_pages = []
            self._cuai_inputs = []
            self._cuai_reads = []
            self._cuai_reads_omitted = 0
            self._cuai_read_document = None
            self._qualification_owned_pages = set()
            self._qualification_opening = None
            self._qualification_frames = {}
            self._qualification_observed_frames = []
            self._qualification_download_pending = None
            self._qualification_redirect = None
            if self._qualification_download_dir is not None:
                self._qualification_download_dir.cleanup()
                self._qualification_download_dir = None
        finally:
            try:
                self._stop_factory()
            finally:
                if self._temporary_profile is not None:
                    self._temporary_profile.cleanup()
