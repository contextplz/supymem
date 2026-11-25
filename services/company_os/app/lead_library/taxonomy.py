"""Fixed, versioned Lead Library taxonomy. Classifiers may only emit values defined here."""
from __future__ import annotations

import re

VERSION = "lead-library-taxonomy-v1"
LICENSE_SOURCE = "SupyMem_GTM_Leads_Final.xlsx (purchased, founder-licensed)"
VERIFIED_SOURCE = "supymem_verified"

LEAD_TYPES = ("customer_prospect", "partner", "mentor", "investor", "founder", "operator", "ecosystem", "media", "government", "researcher")
FUNCTIONS = ("engineering", "product", "sales", "marketing", "operations", "finance", "hr", "legal", "founder_exec", "it", "data_ai",
    "design", "healthcare", "education", "government", "other")
SENIORITIES = ("founder_owner", "c_level", "vp", "director", "manager", "ic", "unknown")
COMPANY_STAGES = ("idea", "startup", "scaleup", "enterprise", "public_sector", "nonprofit", "unknown")
CHANNELS = ("email", "linkedin", "x", "phone")
QUALITY_FLAGS = ("website_is_publisher", "email_is_publisher", "email_generic", "email_placeholder", "email_free", "email_shared",
    "email_domain_mismatch", "email_not_personal", "linkedin_is_company", "contact_company_level", "missing_role", "geography_unknown")
EXTRA_QUALITY_FLAGS = ("name_not_person", "role_unclear", "company_unclear", "lead_type_ambiguous", "not_b2b_relevant")

# Ordered: first match wins for the primary industry; a lead can carry several.
INDUSTRY_RULES: tuple[tuple[str, str], ...] = (
    ("accelerators_ecosystem", r"accelerat|incubat|\btbi\b|nidhi|startup_program|birac|meity|tide\b|samridh|t-?hub|\bciie|nsrcel|bionest|"
        r"\bsine\b|\bsiic|iitmic|iimcip|villgro|axilor|startuptn|elevate|startup_awards|awards|cohort|ecosystem|founder_speaker|speakers?\b|"
        r"maker village|catalyst|social_alpha|pieds|coworking|startupfox|nasscom|kerala startup|ksum|\bstpi\b|\bdivyasampark|\bihub|i-hub"),
    ("venture_capital", r"venture|\bvcs?\b|investor|angel|gp_partner|vc_partner|fund\b|capital|investment"),
    ("government_public_sector", r"govern(?!ance)|govtech|public_sector|district_office|\bsba\b|sbdc|\bscore\b|mbda|useac|\bmep\b|muni_it|county_it|"
        r"state_system|\bmsme_di|\bnsic\b|sidbi|apeda|mpeda|\bepch\b|gjepc|spices_board|coffee_board|tea_?board|chamber|wbc\b|minority|"
        r"trade\b|export|fieo|eepc|cluster\b|ministry"),
    ("neurotech_bci", r"\bbci\b|neuro|\beeg\b|brain"),
    ("accessibility_assistive", r"assistive|accessib|prosthe|orthotic|braille|hearing|vision_assistive|vision assistive|exoskeleton|bionic|myoelectric|"
        r"wheelchair|disab"),
    ("eldercare_agetech", r"elder|agetech|\baging|\bageing|fall detection|fall risk|senior care|remote monitoring"),
    ("cybersecurity", r"cyber|security|\bciso\b|hardware_security|infosec|\bsoc\b|zero.?trust|compliance_ai_sec"),
    ("devtools", r"devtool|developer|devops|\bapi\b|coding_agent|code_eval|sdk|platform_ops|\bcloud\b|infrastructure|ai_infra|agent_infra|"
        r"chat_app_infra|cpaas|open.?source|saas_builder|aiops|b2b -> infrastructure|india_saas_devtools"),
    ("ai_ml", r"\bai\b|artificial|governance|agent|\bllm|genai|generative|machine learning|\bml\b|voice_ai|vernacular|memory|knowledge|"
        r"reinforcement|second_brain|pkm|conversational|speech|tts\b|chief_ai|caio"),
    ("fintech", r"fintech|payments?|banking|bank\b|neobank|lending|credit|financ|accounting|bfsi|wealth|asset management|gst|tax|"
        r"credit_union|exchange"),
    ("insurtech", r"insur"),
    ("healthtech", r"health|medical|clinic|hospital|telemed|wellness|fitness|sleep|\bmental|pharmacy|diagnos"),
    ("medtech_devices", r"medtech|medical device|wearable|fda|smart ring|smart glasses|rehab|consumer_health_hardware"),
    ("biotech_pharma", r"biotech|biolog|bioscien|biopharma|pharma|drug|therapeut|life ?sciences|genom"),
    ("edtech", r"edtech|education|edu\b|higher_ed|school|university|college|learning|tutor|academy"),
    ("hr_tech", r"\bhr\b|hr_|hrtech|hr tech|human resources|recruit|talent|workforce|payroll|hiring"),
    ("legaltech", r"legal|law\b|compliance\b|regtech|gst_compliance|india_compliance"),
    ("martech_adtech", r"martech|marketing|adtech|advertis|seo\b|\bads\b"),
    ("sales_tech", r"sales|\bcrm\b|gtm|revenue|lead gen|customer_support|support_ai|dealership"),
    ("ecommerce_retail", r"e-?commerce|retail|kirana|\bpos\b|store|shop|agentic_commerce|quick commerce"),
    ("d2c_consumer_goods", r"d2c|dtc|consumer goods|fmcg|beauty|personal care|apparel|fashion|jewel|cosmetic|offline_products|multi-sku|"
        r"handicraft|leather"),
    ("textiles_apparel", r"textile|garment"),
    ("food_beverage", r"food|beverage|f&b|restaurant|coffee|tea\b|spice|seafood|dairy|bakery|cloud kitchen"),
    ("agritech", r"agri|farm|livestock|crop|fishery|aqua"),
    ("climate_energy", r"climat|cleantech|clean ?tech|energy|solar|hydrogen|battery|carbon|sustainab|circular|water|sanitation|utility|"
        r"climtech|renewable"),
    ("mobility_automotive", r"mobility|\bev\b|electric vehicle|automotive|automobile|vehicle|transport|dealership"),
    ("logistics_supply_chain", r"logistic|supply ?chain|freight|shipping|warehouse|fleet|delivery"),
    ("manufacturing_industrial", r"manufactur|industrial|factory|\bmsme\b|additive|\bcad\b|engineering_design|engineering, product|materials|"
        r"\bmro\b|plant\b"),
    ("hardware_iot", r"hardware|\biot\b|iot_|embedded|electronic|device|sensor|wearable|hardtech|deeptech"),
    ("robotics", r"robot|physical_ai|drone|autonomous"),
    ("semiconductors", r"semiconductor|chip|\bvlsi\b|fab\b"),
    ("space_aerospace", r"\bspace\b(?![- ]as[- ]a)|spacetech|aerospace|aviation|satellite|rocket"),
    ("defense", r"defen[cs]e|dual_use|military"),
    ("construction_real_estate", r"construction|real estate|proptech|housing|property|architecture|\bmep\b"),
    ("media_content", r"media|content|publish|news|podcast|film|\bott\b"),
    ("gaming", r"gaming|esports|game"),
    ("creator_economy", r"creator|influenc"),
    ("travel_hospitality", r"travel|hospitality|hotel|tourism|leisure"),
    ("events_community", r"event|community|wedding|social_meetup|meetup|irl|conference|social / events|sports_community"),
    ("telecom", r"telecom|network\b|5g|whatsapp"),
    ("it_services_consulting", r"it_services|it services|consult|outsourc|professional_services|services\b|agency|systems integrat"),
    ("enterprise_it", r"\bcio\b|\bcdio\b|\bcdo\b|\bcto\b|enterprise_it|enterprise cxo|enterprise_cxo|tech_cxo|fortune500|higher_ed_it|"
        r"general_enterprise|enterprise_software|\bit\b"),
    ("web3_crypto", r"web3|crypto|blockchain|defi\b|\bnft"),
    ("research_academia", r"research|academi|professor|\blab\b|institute|university"),
    ("nonprofit_social_impact", r"nonprofit|non-profit|\bngo\b|foundation|social impact|charit|women"),
    ("marketplaces", r"marketplace"),
    ("productivity_collaboration", r"productivity|collab|workspace|coordination|notes|calendar|ai_productivity"),
    ("b2b_saas_general", r"b2b|saas|software|smb|sme|small_business|vertical_saas"),
    ("consumer_apps", r"b2c|consumer|social\b|dating|lifestyle"),
)
INDUSTRIES = tuple(dict.fromkeys(name for name, _ in INDUSTRY_RULES))
_INDUSTRY_RE = tuple((name, re.compile(pattern.replace("_", " "), re.I)) for name, pattern in INDUSTRY_RULES)

BUYER_TAG_RULES: tuple[tuple[str, str], ...] = (
    ("devtools", r"devtool|developer|sdk|\bapi\b|coding|code_eval|\bcto\b|engineering|vp_eng|head_engineering"),
    ("ai_agents", r"agent"),
    ("ai_infra", r"ai_infra|agent_infra|llm|model|inference|gpu|enterprise_ai_infra|ai_memory_infra"),
    ("ai_evals", r"eval|verification|qa_testing|agent_qa|testing"),
    ("ai_tools", r"\bai\b|chief_ai|caio|generative|genai|artificial"),
    ("data_analytics", r"analytic|\bbi\b|data|\bcdao\b|insight"),
    ("security_tools", r"security|cyber|\bciso\b|infosec"),
    ("compliance", r"complian|regtech|gst|governance|audit|ai_governance"),
    ("hr_software", r"\bhr\b|hr_|hrtech|hr tech|human resources|workforce|payroll"),
    ("recruiting", r"recruit|talent|hiring"),
    ("sales_tools", r"sales|gtm|\bcrm\b|revenue|lead gen|dealership"),
    ("marketing_tools", r"marketing|martech|\bcmo\b|growth|brand|seo\b|content|creator"),
    ("customer_support", r"customer_support|support_ai|customer success|helpdesk"),
    ("payments", r"payment|upi|checkout|neobank"),
    ("finance_software", r"fintech|accounting|finance|\bcfo\b|banking|lending|credit|bfsi|insur"),
    ("fundraising_help", r"founder|startup|accelerat|incubat|pre-?seed|seed"),
    ("deal_flow", r"venture|\bvcs?\b|investor|angel|gp_partner|capital|fund\b"),
    ("mentoring", r"mentor|advisor|operator_advisor"),
    ("startup_programs", r"accelerat|incubat|\btbi\b|nidhi|startup_program|cohort|birac|meity|tide\b|t-?hub"),
    ("cloud_devops", r"devops|cloud|platform_ops|\bsre\b|aiops|kubernetes"),
    ("enterprise_it", r"\bcio\b|\bcdio\b|\bcdo\b|enterprise_it|it_services|\bit\b|tech_cxo|enterprise cxo|enterprise_cxo|fortune500"),
    ("iot_platform", r"\biot\b|iot_|embedded|sensor|industrial iot"),
    ("hardware_components", r"hardware|electronic|semiconductor|chip|device|wearable|hardtech"),
    ("healthcare_it", r"health|hospital|clinic|medical|patient"),
    ("eldercare_products", r"elder|agetech|\baging|\bageing|\bfall (?:detection|risk|prevention)|assistive|hearing|prosthe"),
    ("education_tools", r"edtech|education|edu\b|higher_ed|school|learning|college"),
    ("legal_tools", r"legal|law\b|contract"),
    ("logistics_software", r"logistic|supply ?chain|freight|fleet|warehouse"),
    ("erp_operations", r"\berp\b|operations|\bcoo\b|ops\b|msme|sme erp|inventory|procure"),
    ("ecommerce_tools", r"commerce|d2c|retail|kirana|marketplace|shop"),
    ("design_tools", r"design|\bcad\b|creative|figma"),
    ("productivity", r"productivity|collab|workspace|coordination|knowledge|memory|second_brain|pkm|notes"),
    ("voice_ai", r"\bvoice|speech|tts\b|\bcall"),
    ("vernacular_ai", r"vernacular|bharat|indic|regional language"),
    ("construction_software", r"construction|proptech|real estate|architecture"),
    ("energy_climate", r"climat|cleantech|energy|solar|carbon|\bev\b|battery|sustainab"),
    ("agriculture_tools", r"agri|farm|livestock|dairy"),
    ("manufacturing_software", r"manufactur|industrial|factory|additive"),
    ("robotics_tools", r"robot|physical_ai|drone"),
    ("government_programs", r"govern|govtech|public_sector|district|\bsba\b|sbdc|ministry|state_"),
    ("export_services", r"export|trade\b|apeda|mpeda|epch|gjepc|fieo|eepc"),
    ("community_events", r"event|community|meetup|conference|speaker|wedding"),
    ("media_partnerships", r"media|publish|podcast|news|influenc"),
    ("insurance_tools", r"insur"),
    ("space_tech", r"space|aerospace|satellite"),
    ("defense_tech", r"defen[cs]e|dual_use"),
)
BUYER_TAGS = tuple(dict.fromkeys(name for name, _ in BUYER_TAG_RULES))
LABELS = {tag: tag.replace("_", " ") for tag in (*INDUSTRIES, *BUYER_TAGS, *FUNCTIONS)} | {
    "ai_ml": "AI/ML", "ai_tools": "AI tools", "devtools": "developer tools",
    "ai_agents": "AI agents", "ai_infra": "AI infrastructure", "ai_evals": "AI evaluations",
    "data_ai": "data and AI", "cloud_devops": "cloud and DevOps", "enterprise_it": "enterprise IT",
    "healthcare_it": "healthcare IT", "hr": "HR", "hr_tech": "HR technology", "hr_software": "HR software",
    "iot_platform": "IoT platforms", "erp_operations": "ERP and operations", "founder_exec": "founders and executives",
    "d2c_consumer_goods": "consumer goods", "voice_ai": "voice AI", "vernacular_ai": "regional language AI",
}
_BUYER_RE = tuple((name, re.compile(pattern.replace("_", " "), re.I)) for name, pattern in BUYER_TAG_RULES)

FUNCTION_TAGS = {
    "founder_exec": ("fundraising_help", "sales_tools", "productivity"), "engineering": ("devtools", "cloud_devops", "ai_tools"),
    "it": ("enterprise_it", "security_tools", "data_analytics"), "data_ai": ("ai_tools", "data_analytics", "ai_infra"),
    "product": ("productivity", "data_analytics", "design_tools"), "design": ("design_tools", "productivity"),
    "sales": ("sales_tools", "customer_support", "data_analytics"), "marketing": ("marketing_tools", "data_analytics", "media_partnerships"),
    "finance": ("finance_software", "data_analytics"), "hr": ("hr_software", "recruiting"), "legal": ("legal_tools", "compliance"),
    "operations": ("erp_operations", "productivity"), "healthcare": ("healthcare_it",), "education": ("education_tools",),
    "government": ("government_programs",),
}
INDUSTRY_TAGS = {
    "venture_capital": ("deal_flow", "mentoring"), "accelerators_ecosystem": ("startup_programs", "mentoring", "community_events"),
    "government_public_sector": ("government_programs",), "cybersecurity": ("security_tools", "compliance"), "devtools": ("devtools", "cloud_devops"),
    "ai_ml": ("ai_tools", "ai_agents"), "fintech": ("finance_software", "payments"), "healthtech": ("healthcare_it",),
    "medtech_devices": ("hardware_components", "healthcare_it"), "eldercare_agetech": ("eldercare_products", "healthcare_it"),
    "edtech": ("education_tools",), "hr_tech": ("hr_software", "recruiting"), "legaltech": ("legal_tools", "compliance"),
    "ecommerce_retail": ("ecommerce_tools", "payments"), "d2c_consumer_goods": ("ecommerce_tools", "marketing_tools"),
    "hardware_iot": ("hardware_components", "iot_platform"), "robotics": ("robotics_tools", "hardware_components"),
    "climate_energy": ("energy_climate",), "logistics_supply_chain": ("logistics_software",), "agritech": ("agriculture_tools",),
    "manufacturing_industrial": ("manufacturing_software", "erp_operations"), "enterprise_it": ("enterprise_it", "security_tools"),
}

RAW_LEAD_TYPES = {
    "customer_prospect": "customer_prospect", "partner_prospect": "partner", "mentor": "mentor", "mentor_incubator": "mentor",
    "mentor_advisor": "mentor", "advisor": "mentor", "investor": "investor", "founder": "founder", "operator": "operator",
    "ecosystem_partner": "ecosystem", "researcher": "researcher", "influencer": "media", "speaker": "ecosystem",
}

_C = r"(?:^|[^a-z])"
FUNCTION_RULES: tuple[tuple[str, str], ...] = (
    ("founder_exec", _C + r"(?:co-?\s?)?founder|founding (?:ceo|cto|partner|team)|\bceo\b|chief executive|(?<!vice )(?<!vice-)president|chair(?:man|person)?|"
        r"managing director|\bowner\b|proprietor|general manager|\bmd\b|executive director|chief of staff|founder office|co-ceo"),
    ("data_ai", r"\bai\b|artificial intelligence|machine learning|\bml\b|data scien|\bdata\b|analytic|\bcdao\b|\bcaio\b"),
    ("it", r"\bcio\b|\bcdio\b|information officer|information technology|\bit\b|digital officer|\bcdo\b|infrastructure|systems"),
    ("engineering", r"\bcto\b|technology officer|engineer|developer|(?<!biz )\bdev\b|architect|technical|tech lead|\bsre\b|devops|security|\bciso\b|"
        r"\bcso\b|research(?:er)? engineer|\bvp eng"),
    ("product", r"\bcpo\b|product|\bpm\b|\bcpto\b"),
    ("design", r"design|\bux\b|\bui\b|creative"),
    ("sales", r"sales|\bcro\b|revenue|business development|biz ?dev|\bbd\b|account (?:executive|manager)|partnership|commercial"),
    ("marketing", r"marketing|\bcmo\b|growth|brand|community|content|communications|\bpr\b|evangel|devrel"),
    ("finance", r"\bcfo\b|finance|financial|accountant|accounting|controller|treasur|investment|investor|venture|partner|principal|"
        r"associate|analyst|\bgp\b|\blp\b|angel"),
    ("hr", r"\bhr\b|human resources|people|talent|recruit|chro"),
    ("legal", r"legal|counsel|lawyer|attorney|compliance|secretary"),
    ("operations", r"\bcoo\b|operations|operating|\bops\b|supply|logistics|program|manager|liaison|spoc|coordinator|incubat|admin"),
    ("healthcare", r"medical|physician|doctor|\bdr\b|clinical|nurse|health"),
    ("education", r"professor|teacher|faculty|dean|lecturer|academic|educat|principal investigator|\bpi\b"),
    ("government", r"director general|deputy director|assistant director|district director|state director|jurisdictional|commissioner|"
        r"officer|ministerial|government|secretary"),
)
_FUNCTION_RE = tuple((name, re.compile(pattern, re.I)) for name, pattern in FUNCTION_RULES)

SENIORITY_RULES: tuple[tuple[str, str], ...] = (
    ("founder_owner", _C + r"(?:co-?\s?)?founder|founding|\bowner\b|proprietor|solo founder"),
    ("c_level", r"\bc[a-z]{1,3}o\b|chief|(?<!vice )(?<!vice-)\bpresident\b|chair(?:man|person)?|managing (?:director|partner)|general partner|\bgp\b|"
        r"executive director|\bmd\b|co-ceo|\bpartner\b|director general"),
    ("vp", r"\bvp\b|vice president|\bsvp\b|\bevp\b|head of|\bhead\b"),
    ("director", r"director|principal"),
    ("manager", r"manager|lead\b|leader|\bagm\b|\bdgm\b|\bam\b|supervisor|coordinator|spoc|liaison officer|officer"),
    ("ic", r"engineer|developer|analyst|associate|specialist|scientist|researcher|designer|consultant|assistant|member|\bintern\b|"
        r"professor|faculty|builder|maker|innovator"),
)
_SENIORITY_RE = tuple((name, re.compile(pattern, re.I)) for name, pattern in SENIORITY_RULES)

# A statutory board (Tea Board, Coir Board), not a circuit board, dashboard, or someone's board seat.
_BOARD = r"(?<!circuit )(?<!circuit-)\bboard\b(?! member| of directors| advis| seat| game)"
STAGE_RULES: tuple[tuple[str, str], ...] = (
    ("public_sector", r"govern(?!ance)|govtech|\bpublic_sector|district_office|\bsba\b|sbdc|muni_it|county_it|state_system|\bnsic\b|sidbi|apeda|"
        r"mpeda|" + _BOARD + r"|ministry|\.gov\b|gov\.in|nic\.in|community_college|msme_di|\bmbda\b|useac"),
    ("nonprofit", r"nonprofit|non-profit|\bngo\b|foundation|charit|\.ngo\b|\.org\.in"),
    ("enterprise", r"enterprise cxo|enterprise_cxo|fortune500|annual_report|it_services|conglomerate|general_enterprise|tech_cxo|"
        r"national_cio|cio_moves|enterprise_it|higher_ed_it|\bpublic company\b(?! sources)|listed company|stock exchange"),
    ("scaleup", r"soonicorn|unicorn|series [b-e]|scaleup|scale-up|growth stage|late[- ]stage|tech30|"
        r"\$\s?\d{3,}(?:\.\d+)?\s?m\b|\$\s?\d+(?:\.\d+)?\s?b(?:n|illion)?\b"),
    ("startup", r"startup|yc|y combinator|ycombinator|techstars|500_global|500\.co|accelerat|incubat|portfolio|product_hunt|seed|"
        r"shark_?tank|antler|entrepreneur_first|\btbi\b|nidhi|birac|founder|indie|early_saas|alchemist|peakxv|surge|pre-?seed|"
        r"wikidata_india_founder"),
    ("idea", r"\bidea stage|pre-product|stealth"),
)
_STAGE_RE = tuple((name, re.compile(pattern, re.I)) for name, pattern in STAGE_RULES)

FREE_MAIL = frozenset({"gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.in", "yahoo.in", "ymail.com", "hotmail.com", "outlook.com",
    "live.com", "msn.com", "icloud.com", "me.com", "mac.com", "aol.com", "protonmail.com", "proton.me", "pm.me", "gmx.com", "gmx.de",
    "zoho.com", "zohomail.com", "zohomail.in", "rediffmail.com", "mail.com", "yandex.com", "yandex.ru", "fastmail.com", "hey.com",
    "tutanota.com", "qq.com", "163.com", "web.de", "hotmail.co.uk", "yahoo.co.uk", "outlook.in", "live.in"})
PLACEHOLDER_DOMAINS = frozenset({"example.com", "example.org", "example.net", "company.com", "domain.com", "email.com", "test.com",
    "yourcompany.com", "yourdomain.com", "mycompany.com", "sample.com", "acme.com", "website.com", "xyz.com", "abc.com", "none.com",
    "noemail.com", "na.com", "email.address"})
PLACEHOLDER_LOCALS = frozenset({"name", "firstname", "first.last", "firstname.lastname", "john.doe", "jane.doe", "your.name", "yourname",
    "user", "username", "email", "test", "xxx", "abc", "na", "none", "noreply", "no-reply", "donotreply"})
GENERIC_LOCALS = frozenset({"info", "hello", "contact", "contactus", "contact-us", "support", "help", "admin", "office", "team", "sales",
    "marketing", "enquiry", "enquiries", "inquiry", "inquiries", "careers", "jobs", "hr", "press", "media", "pr", "partnerships",
    "partners", "business", "bd", "biz", "mail", "general", "reception", "service", "services", "customer", "customercare",
    "care", "feedback", "hi", "hey", "founders", "founder", "investors", "ir", "legal", "privacy", "security", "billing", "accounts",
    "finance", "ops", "operations", "community", "events", "newsletter", "news", "startups", "startup", "incubation", "incubator",
    "director", "dir", "secretary", "registrar", "webmaster", "web", "it", "tech", "connect", "talk", "ask", "apply", "grants", "program",
    "programs", "nidhi", "tbi", "ceo", "corporate", "corporate.sustainability", "investor.relations", "investorrelations", "office.ceo",
    "cs", "compliance", "grievance", "rti", "pio", "helpdesk", "query", "queries", "online", "digital", "write", "writeto", "team.hello"})
PUBLISHER_HOSTS = frozenset({"inc42.com", "yourstory.com", "linkedin.com", "twitter.com", "x.com", "t.co", "github.com", "facebook.com",
    "instagram.com", "youtube.com", "bit.ly", "linktr.ee", "plus.google.com", "medium.com", "substack.com", "forbesindia.com",
    "forbes.com", "techcrunch.com", "crunchbase.com", "angel.co", "wellfound.com", "producthunt.com", "ycombinator.com",
    "startupfeed.in", "indianstartuptimes.com", "startuptalky.com", "cxojunction.com", "vcbacked.co", "economictimes.indiatimes.com",
    "business-standard.com", "livemint.com", "moneycontrol.com", "entrackr.com", "vccircle.com", "tracxn.com", "wikipedia.org",
    "query.wikidata.org", "wikidata.org", "sevalla.com", "f6s.com", "google.com", "sites.google.com", "notion.site", "wix.com",
    "reddit.com", "news.ycombinator.com", "500.co", "techstars.com", "accel.com", "atoms.accel.com", "surge.peakxv.com", "antler.co",
    "a16z.com", "junior.adamlead.com", "theorg.com", "zoominfo.com", "rocketreach.co", "apollo.io", "signalhire.com"})

COUNTRY_ALIASES = {
    "IN": ("india", "bharat", "in", "ind"), "US": ("united states", "usa", "us", "u.s.", "u.s.a.", "united states of america", "america",
        "united_states"),
    "GB": ("united kingdom", "uk", "u.k.", "england", "scotland", "wales", "great britain", "britain", "gb", "northern ireland"),
    "CA": ("canada",), "AU": ("australia",), "SG": ("singapore",), "DE": ("germany", "deutschland"), "FR": ("france",),
    "NL": ("netherlands", "the netherlands", "holland"), "IL": ("israel",), "CH": ("switzerland",), "FI": ("finland",),
    "SE": ("sweden",), "NO": ("norway",), "DK": ("denmark",), "IE": ("ireland",), "ES": ("spain",), "IT": ("italy",), "PT": ("portugal",),
    "BE": ("belgium",), "AT": ("austria",), "PL": ("poland",), "EE": ("estonia",), "LT": ("lithuania",), "LV": ("latvia",),
    "CZ": ("czech republic", "czechia"), "HU": ("hungary",), "RO": ("romania",), "GR": ("greece",), "UA": ("ukraine",),
    "AE": ("uae", "united arab emirates", "dubai", "abu dhabi"), "SA": ("saudi arabia", "ksa"), "QA": ("qatar",), "JP": ("japan",),
    "KR": ("south korea", "korea"), "CN": ("china",), "HK": ("hong kong",), "TW": ("taiwan",), "ID": ("indonesia",), "MY": ("malaysia",),
    "TH": ("thailand",), "VN": ("vietnam",), "PH": ("philippines",), "BD": ("bangladesh",), "PK": ("pakistan",), "LK": ("sri lanka",),
    "NP": ("nepal",), "NZ": ("new zealand",), "BR": ("brazil",), "MX": ("mexico",), "AR": ("argentina",), "CL": ("chile",),
    "CO": ("colombia",), "NG": ("nigeria",), "KE": ("kenya",), "ZA": ("south africa",), "EG": ("egypt",), "TR": ("turkey", "türkiye"),
    "LU": ("luxembourg",), "IS": ("iceland",), "CY": ("cyprus",), "MT": ("malta",), "SI": ("slovenia",), "SK": ("slovakia",),
    "HR": ("croatia",), "BG": ("bulgaria",), "RS": ("serbia",),
}
EU_UK = frozenset({"GB", "DE", "FR", "NL", "CH", "FI", "SE", "NO", "DK", "IE", "ES", "IT", "PT", "BE", "AT", "PL", "EE", "LT", "LV", "CZ",
    "HU", "RO", "GR", "LU", "IS", "CY", "MT", "SI", "SK", "HR", "BG"})
US_STATES = {"alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA", "colorado": "CO", "connecticut": "CT",
    "delaware": "DE", "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI",
    "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV", "new hampshire": "NH",
    "new jersey": "NJ", "new mexico": "NM", "new york": "NY", "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN",
    "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV", "wisconsin": "WI",
    "wyoming": "WY", "district of columbia": "DC", "washington dc": "DC", "washington, dc": "DC", "puerto rico": "PR"}
IN_STATES = ("andhra pradesh", "arunachal pradesh", "assam", "bihar", "chhattisgarh", "goa", "gujarat", "haryana", "himachal pradesh",
    "jharkhand", "karnataka", "kerala", "madhya pradesh", "maharashtra", "manipur", "meghalaya", "mizoram", "nagaland", "odisha", "orissa",
    "punjab", "rajasthan", "sikkim", "tamil nadu", "telangana", "tripura", "uttar pradesh", "uttarakhand", "west bengal", "delhi",
    "jammu and kashmir", "ladakh", "puducherry", "chandigarh", "delhi ncr", "ncr")
CITY_COUNTRY = {
    **dict.fromkeys(("bengaluru", "bangalore", "mumbai", "bombay", "hyderabad", "pune", "ahmedabad", "gurugram", "gurgaon", "chennai",
        "surat", "delhi", "new delhi", "delhi ncr", "noida", "indore", "jaipur", "kochi", "cochin", "lucknow", "kolkata", "bhubaneswar",
        "coimbatore", "chandigarh", "vadodara", "nagpur", "thiruvananthapuram", "trivandrum", "visakhapatnam", "vizag", "mysuru", "mysore",
        "bhopal", "patna", "kanpur", "guwahati", "ranchi", "rajkot", "mangaluru", "mangalore", "madurai", "nashik", "goa", "dehradun",
        "ludhiana", "amritsar", "varanasi", "raipur", "bhilai", "jodhpur", "udaipur", "kozhikode", "calicut", "thrissur", "manipal",
        "hubli", "belgaum", "belagavi", "vijayawada", "tirupati", "warangal", "aurangabad", "navi mumbai", "thane", "faridabad",
        "ghaziabad", "greater noida", "mohali", "kharagpur", "roorkee", "kanpur", "mandi", "ropar", "gandhinagar", "anand", "manesar"), "IN"),
    **dict.fromkeys(("san francisco", "sf bay area", "bay area", "new york city", "nyc", "brooklyn", "boston", "seattle", "los angeles",
        "chicago", "cambridge, ma", "austin", "denver", "boulder", "atlanta", "palo alto", "mountain view", "menlo park", "san jose",
        "sunnyvale", "oakland", "berkeley", "san mateo", "redwood city", "santa clara", "san diego", "miami", "dallas", "houston",
        "philadelphia", "pittsburgh", "washington dc", "detroit", "minneapolis", "portland", "salt lake city", "nashville", "raleigh",
        "phoenix", "las vegas", "baltimore", "st. louis", "columbus", "cleveland", "cincinnati", "indianapolis", "kansas city",
        "milwaukee", "charlotte", "tampa", "orlando", "sacramento", "irvine", "santa monica", "new haven", "princeton", "ann arbor",
        "madison", "durham", "silicon valley"), "US"),
    **dict.fromkeys(("london", "manchester", "edinburgh", "oxford", "bristol", "glasgow", "birmingham", "leeds", "belfast"), "GB"),
    **dict.fromkeys(("berlin", "munich", "hamburg", "frankfurt", "cologne"), "DE"), **dict.fromkeys(("paris", "lyon"), "FR"),
    **dict.fromkeys(("amsterdam", "rotterdam", "eindhoven", "delft"), "NL"), **dict.fromkeys(("toronto", "vancouver", "montreal", "waterloo",
        "ottawa", "calgary"), "CA"), **dict.fromkeys(("sydney", "melbourne", "brisbane"), "AU"), **dict.fromkeys(("tel aviv",), "IL"),
    **dict.fromkeys(("zurich", "geneva", "lausanne"), "CH"), **dict.fromkeys(("stockholm",), "SE"), **dict.fromkeys(("helsinki",), "FI"),
    **dict.fromkeys(("dublin",), "IE"), **dict.fromkeys(("madrid", "barcelona"), "ES"), **dict.fromkeys(("copenhagen",), "DK"),
    **dict.fromkeys(("tallinn",), "EE"), **dict.fromkeys(("lisbon",), "PT"), **dict.fromkeys(("tokyo",), "JP"),
    **dict.fromkeys(("seoul",), "KR"), **dict.fromkeys(("dubai", "abu dhabi"), "AE"), **dict.fromkeys(("jakarta",), "ID"),
    **dict.fromkeys(("lagos",), "NG"), **dict.fromkeys(("nairobi",), "KE"), **dict.fromkeys(("sao paulo", "são paulo"), "BR"),
}
_US_ABBR = frozenset(US_STATES.values())
_ALIAS_TO_CODE = {alias: code for code, aliases in COUNTRY_ALIASES.items() for alias in aliases}
_UNKNOWN_GEO = frozenset({"", "other", "unknown", "remote", "global", "worldwide", "n/a", "na", "none", "-", "various", "multiple",
    "international", "europe", "eu", "asia", "apac", "emea", "latam", "north america", "middle east", "africa"})


def parse_geography(raw: str | None) -> dict:
    """Deterministic: {country_code, region, city}; unknown parts are None. The raw string is kept separately."""
    text = re.sub(r"\s+", " ", str(raw or "")).strip()
    lowered = text.lower()
    out = {"country_code": None, "region": None, "city": None}
    if lowered in _UNKNOWN_GEO:
        if lowered in {"europe", "eu"}:
            out["region"] = "Europe"
        return out
    parts = [part.strip(" .") for part in re.split(r"[|,;/()]| - ", text) if part.strip(" .")]
    low = [part.lower() for part in parts]
    for part in reversed(low):
        if part in _ALIAS_TO_CODE:
            out["country_code"] = _ALIAS_TO_CODE[part]
            break
    for part, original in zip(low, parts):
        if part in US_STATES or (original.isupper() and original in _US_ABBR and out["country_code"] in {None, "US"}):
            out["country_code"] = out["country_code"] or "US"
            out["region"] = out["region"] or (US_STATES.get(part) or original)
        elif part in IN_STATES:
            out["country_code"] = out["country_code"] or "IN"
            out["region"] = out["region"] or original.title()
    for part, original in zip(low, parts):
        city = part.removesuffix(" city") if part not in CITY_COUNTRY else part
        if city in CITY_COUNTRY and (out["country_code"] in {None, CITY_COUNTRY[city]}):
            out["country_code"] = CITY_COUNTRY[city]
            out["city"] = original
            break
    if out["country_code"] == "IN" and out["region"] is None and out["city"]:
        out["region"] = None
    if not out["country_code"] and not out["city"] and len(parts) == 1 and lowered in {"new york", "ny"}:
        out.update(country_code="US", region="NY")
    return out


def tokens(*values) -> list[str]:
    seen: dict[str, None] = {}
    for value in values:
        for token in re.split(r"\||,|;|->|\s/\s", str(value or "")):
            token = token.strip().lower()
            if token and token not in {"none", "null"}:
                seen[token] = None
    return list(seen)


# Too broad to describe a company alone; notes may add the vertical (an "AI" tagged invoice financer is fintech too).
_GENERIC_INDUSTRIES = frozenset({"b2b_saas_general", "consumer_apps", "ai_ml"})
# Directory boilerplate in notes that names the source or the founder's school, not what the company does.
_NOTE_NOISE = re.compile(r"(?i:developer of|sanity(?: api)?|portfolio (?:api|feed|graph)|accelerat\w*|incubat\w*|space[- ]as[- ]a[- ]service)|"
    r"\b(?:University|College|Institute) of [A-Z][\w ]{0,30}|\b[A-Z][\w.&]* (?:University|College|Institute)\b")


def industries_for(text_tokens: list[str], extra_text: str = "") -> list[str]:
    found: dict[str, None] = {}
    for token in (token.replace("_", " ") for token in text_tokens):
        for name, pattern in _INDUSTRY_RE:
            if pattern.search(token):
                found[name] = None
                break
    if extra_text and not set(found) - _GENERIC_INDUSTRIES:
        extra_text = _NOTE_NOISE.sub(" ", extra_text.replace("_", " "))
        for name, pattern in _INDUSTRY_RE:
            if name not in found and name not in {"b2b_saas_general", "consumer_apps", "enterprise_it", "research_academia",
                    "events_community", "nonprofit_social_impact", "government_public_sector", "venture_capital",
                    "accelerators_ecosystem"} and pattern.search(extra_text):
                found[name] = None
    specific = [name for name in found if name not in {"b2b_saas_general", "consumer_apps"}]
    return (specific or list(found))[:4]


def buyer_tags_for(text: str) -> list[str]:
    text = text.replace("_", " ")
    return [name for name, pattern in _BUYER_RE if pattern.search(text)][:8]


def function_for(role: str | None) -> str:
    role = (role or "").strip()
    if not role:
        return "other"
    role += " " + re.sub(r"(?<=[a-z])s\b", "", role, flags=re.I)
    for name, pattern in _FUNCTION_RE:
        if pattern.search(role):
            return name
    return "other"


def seniority_for(role: str | None) -> str:
    role = (role or "").strip()
    if not role:
        return "unknown"
    role += " " + re.sub(r"(?<=[a-z])s\b", "", role, flags=re.I)
    for name, pattern in _SENIORITY_RE:
        if pattern.search(role):
            return name
    return "unknown"


def stage_for(text: str) -> str:
    for name, pattern in _STAGE_RE:
        if pattern.search(text):
            return name
    return "unknown"


_INVESTOR = re.compile(r"venture|\bvcs?\b|investor|angel|gp_partner|vc_partner|investment|capital|fund\b", re.I)
_INVESTOR_ROLE = re.compile(r"partner|investor|principal|associate|investment|venture|analyst|\bgp\b|angel", re.I)
_ECOSYSTEM = re.compile(r"accelerat|incubat|\btbi\b|nidhi|startup_program|ecosystem|cohort|t-?hub|tide\b|samridh|program", re.I)
_GOV = re.compile(r"govern(?!ance)|govtech|public_sector|district_office|\bsba\b|sbdc|\bmbda\b|useac|ministry|" + _BOARD + r"|\bnsic\b|sidbi|"
    r"apeda|mpeda|\bepch\b|gjepc|muni_it|county_it|state_system|wbc\b|minority|chamber|trade\b|export", re.I)
_RESEARCH = re.compile(r"professor|researcher|scientist|faculty|\bpi\b|research", re.I)
_MEDIA = re.compile(r"influenc|journalist|editor|podcast|creator|writer|media", re.I)
_FOUNDER = re.compile(_C + r"(?:co-?\s?)?founder|founding", re.I)


def lead_type_for(raw_type: str | None, raw_types: str | None, role: str | None, text: str) -> tuple[str, float]:
    raw = (raw_type or "").strip().lower()
    role = role or ""
    if _INVESTOR.search(text) and _INVESTOR_ROLE.search(role) and not _FOUNDER.search(role):
        return "investor", 0.85
    if raw in {"mentor", "mentor_incubator", "mentor_advisor", "advisor"}:
        return "mentor", 0.9
    if raw == "investor":
        return "investor", 0.9
    if _GOV.search(text) and raw in {"ecosystem_partner", "interesting_contact", "partner_prospect", "speaker", "customer_prospect"} \
            and not _FOUNDER.search(role) and re.search(r"director|officer|secretary|contact|manager|commissioner|specialist|liaison|"
                r"\bam\b|agm|dgm|assistant|deputy|head|chief", role, re.I):
        return "government", 0.8
    if raw == "ecosystem_partner" or (_ECOSYSTEM.search(text) and re.search(r"contact|spoc|incubat|program|manager|head|ceo of", role, re.I)
            and not _FOUNDER.search(role)):
        return "ecosystem", 0.8 if raw == "ecosystem_partner" else 0.65
    if raw == "researcher" or (_RESEARCH.search(role) and raw in {"interesting_contact", "speaker"}):
        return "researcher", 0.8
    if raw == "influencer" or (raw in {"interesting_contact", "speaker"} and _MEDIA.search(role)):
        return "media", 0.75
    if raw in RAW_LEAD_TYPES:
        mapped = RAW_LEAD_TYPES[raw]
        return mapped, 0.6 if raw == "speaker" else 0.9
    if raw == "interesting_contact":
        if _FOUNDER.search(role):
            return "founder", 0.6
        if "customer_prospect" in (raw_types or ""):
            return "customer_prospect", 0.55
        return "customer_prospect", 0.4
    return "customer_prospect", 0.3


def baseline(lead: dict) -> dict:
    """Deterministic classification of one pre-pass lead. Workers review rows below REVIEW_THRESHOLD."""
    role = lead.get("role_title") or ""
    cat_tokens = tokens(lead.get("category"), lead.get("categories"), lead.get("subcategory"), lead.get("focus_niche"))
    cat_text = " | ".join(cat_tokens)
    notes = " ".join(str(lead.get(key) or "") for key in ("qualification_notes", "score_rationale"))[:600]
    context = " | ".join((cat_text, str(lead.get("discovery_method") or ""), str(lead.get("source_host") or ""), lead.get("website_host") or ""))
    lead_type, type_conf = lead_type_for(lead.get("lead_type_raw"), lead.get("lead_types_raw"), role, cat_text + " " + context)
    function, seniority = function_for(role), seniority_for(role)
    industry = industries_for(cat_tokens, (lead.get("company") or "") + " " + notes)
    if lead_type not in {"ecosystem", "mentor", "investor", "government", "partner"}:
        # An incubatee or award winner is found through the program; the program is not their industry.
        industry = [name for name in industry if name != "accelerators_ecosystem"]
    stage_context = " | ".join((cat_text, str(lead.get("discovery_method") or ""), lead.get("website_host") or ""))
    stage = stage_for(" | ".join((stage_context, notes, role)))
    if stage == "startup" and (lead_type == "investor" or "venture_capital" in cat_text):
        stage = "unknown"
    if lead_type == "government":
        stage = "public_sector"
        if function in {"other", "operations"}:
            function = "government"
    tags = buyer_tags_for(" | ".join((cat_text, role, notes[:300])))
    if lead_type == "investor" and "deal_flow" not in tags:
        tags = ["deal_flow", *tags]
    if lead_type == "mentor" and "mentoring" not in tags:
        tags = ["mentoring", *tags]
    for tag in FUNCTION_TAGS.get(function, ()) + INDUSTRY_TAGS.get(industry[0] if industry else "", ()):
        if len(tags) >= 3:
            break
        if tag not in tags:
            tags.append(tag)
    tags = tags[:8]
    confidence = type_conf
    reasons = []
    if lead.get("name_is_company"):
        confidence = min(confidence, 0.4)
        reasons.append("name")
    if not role.strip() or function == "other":
        confidence = min(confidence, 0.5)
        reasons.append("function")
    if seniority == "unknown":
        confidence = min(confidence, 0.55)
        reasons.append("seniority")
    if not industry or industry == ["b2b_saas_general"] or industry == ["consumer_apps"]:
        confidence = min(confidence, 0.6)
        reasons.append("industry")
    if len(tags) < 3:
        confidence = min(confidence, 0.6)
        reasons.append("buyer_tags")
    if type_conf < 0.7:
        reasons.append("lead_type")
    return {"lead_type": lead_type, "function": function, "seniority": seniority, "industry": industry, "company_stage": stage,
        "buyer_tags": tags, "extra_quality_flags": [], "confidence": round(confidence, 2), "note": "", "review": reasons}


REVIEW_THRESHOLD = 0.7
