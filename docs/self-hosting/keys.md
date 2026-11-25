# Keys and connections

Open **Settings > Keys and connections** to save and test your connections. You can also import a private `.env` from the location shown under **Prefer a file?** on that screen. API and worker import at startup and on settings reads. Restart after a file edit to refresh both processes. Change app-saved fields here instead of expecting imports to overwrite them.

Save `.env` files as UTF-8 for both desktop and source browser mode. Windows PowerShell 5.1 `Out-File` defaults to UTF-16, so choose UTF-8 explicitly in your editor instead.

Keys belong in password fields, not chat, screenshots, logs, or source control. Saved secret fields display their status, never their value. Leave a secret field blank to keep the saved key; enter a replacement to rotate it. Revoke a retired key with its provider. Your input `.env` contains plaintext until you remove its secret entries; verify the import before removing them. A requested service call sends the credential or derived signature needed to authenticate to that service, plus the task inputs listed below; saving a key alone runs no external action.

The tables use catalog identifiers for each connection field and show its exact in-app label. Model catalog fields are saved in the shared model section; they are not separate provider environment settings. Choose **Save your settings** after editing. Connection tests do not send outreach, publish pages, or run connected-app actions; model and paid-search tests can incur provider charges.

## Import precedence and provider selection

Precedence is per field: an app save wins, including an intentionally empty non-secret field. Source runs otherwise prefer process environment, then data-folder `.env`, then checkout `.env`. Desktop import reads only the data-folder `.env`; inherited user keys and checkout `.env` are ignored. Blank entries never erase saved fields. Imported fields refresh on later reads, while app-saved fields stay owned by the app. Multiple aliases in the same file are applied in file order, last nonblank alias wins; process aliases are applied in sorted name order. Use one spelling per field to avoid conflicts.

Files are parsed as data, without variable interpolation or executing shell commands. Unknown names are ignored with a notice naming only the variable. You must use the accepted names below; other names from the repository's developer environment example are not connection imports.

Choose your provider explicitly when you keep multiple accounts. Without a saved or imported selection, model keys are considered in OpenAI, Azure OpenAI, Anthropic, then OpenAI-compatible order. Search import selects Tavily before Exa when both keys are present; a Firecrawl URL alone keeps Built-in search. Publishing import selects S3 before Netlify before Vercel when their keys are present. App-saved provider selections take priority. OpenAI search uses the saved model connection, not another search key.

<!-- connection: providers -->
## Your AI provider

Connect your own AI account, or use a model running on your machine.

**Unlocks:** Idea work, sourced research, customer search, experiment design, and content drafts. See [Known issues](../../README.md#known-issues) for the customer search evidence and retry steps.

Choose OpenAI, Azure OpenAI, Anthropic, or an OpenAI-compatible service. For Azure, **Chat model** is your deployment name and **Service URL** is your resource endpoint. For a local model, choose **OpenAI-compatible**, enter its loopback `/v1` URL and exact model name, and leave **Model API key** blank if the server does not require one.

Get your key from [OpenAI](https://platform.openai.com/api-keys), [Azure](https://portal.azure.com), or [Anthropic](https://console.anthropic.com/settings/keys). Anthropic uses its native Messages API with your Anthropic key; you do not need a compatible endpoint. Choose Anthropic and save your model and key on this screen, then test your connection. Other services use OpenAI-compatible requests. Do not enter a cloud key for a keyless local server.

After saving, choose **Test your connection**. The test makes a model request and your provider can charge for it.

**Data sent:** Your prompts and the context you choose to include go to your selected model service. Local models keep them on your machine.

| Saved field | In-app field | Purpose | `.env` variable names |
|---|---|---|---|
| `providers.provider` | Model service | Choose your model account or a local model server. | `SUPYMEM_LLM_PROVIDER` |
| `providers.model` | Chat model | Enter the model or deployment name from your service. | `SUPYMEM_MODEL_CHAT` |
| `providers.base_url` | Service URL | Enter your Azure resource or local model server address when needed. | `OPENAI_BASE_URL`, `AZURE_OPENAI_ENDPOINT`, `ANTHROPIC_BASE_URL`, `OPENAI_COMPATIBLE_BASE_URL` |
| `providers.api_key` | Model API key | Use a key from your selected model account. Local model servers do not need a key. | `OPENAI_API_KEY`, `AZURE_OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_COMPATIBLE_API_KEY` |
| `providers.context_window` | Context window | For your local model server, enter its token limit (2,048 to 262,144). Supymem uses 8,192 if you leave it blank. | Set in Keys and connections only. |
| `providers.catalog_model` | Azure model ID | If your Azure deployment has a custom name, enter the underlying model ID here. Keep the deployment name in Chat model. | Set in Keys and connections only. |

### Other model roles and runtime imports

The importer also accepts `SUPYMEM_MODEL_PLANNING`, `SUPYMEM_MODEL_EXTRACTION`, `SUPYMEM_MODEL_VISION`, and `SUPYMEM_MODEL_REALTIME`. They set role-specific model names for your selected provider. They are not extra fields in the Keys and connections catalog. Use a model that supports the role you request. For a local server, start it separately and use the installed model name and loopback URL on Your AI provider.

`LLM_OFFLINE` and `LLM_CASSETTE_MODE` are accepted runtime imports, not user keys or feature switches. Sample mode in the app needs neither. A configured real provider overrides the saved offline preference; process-level replay still forces offline calls. Remove replay from your private startup configuration and restart if you want live model requests. Never use sample output as market evidence.

<!-- connection: smtp -->
## Your mail account

Send outreach through your own mailbox and read replies. Your disclosure, opt-out and send limits stay in place.

**Unlocks:** Send email, Read replies. Readiness also depends on the feature switch and its other required settings.

Get the SMTP and IMAP server names from your mailbox provider's account settings. Use an app password or approved mail credential, not your account's main password. For Gmail, enable 2-Step Verification and create an [app password](https://support.google.com/accounts/answer/185833). If your organization disables app passwords, ask its administrator for an approved SMTP/IMAP credential.

SMTP defaults to port 587 with STARTTLS; port 465 uses implicit TLS. IMAP defaults to 993 with TLS, using the same mailbox address and app password. The mailbox address is also your sender address. Enter your sender name and postal address before outreach. **Booking link** is your existing HTTPS scheduling page.

Choose **Test your connection** for outgoing mail and **Test incoming mail** for replies. Neither sends an email. Then switch on **Send email** in **Settings > Capabilities**. Your requested emails retain source disclosure, opt-out, suppression, consent checks, and send caps; guessed addresses are not used. Reply-based opt-out does not require exposing your local app to the internet.

**Data sent:** When you send mail, your mail server receives the recipient, subject, message and attachments. Reading replies uses your incoming mail server. Testing only signs in and sends no mail.

| Saved field | In-app field | Purpose | `.env` variable names |
|---|---|---|---|
| `smtp.host` | Outgoing mail host | Your mailbox provider's SMTP server address. | `SUPYMEM_SMTP_HOST`, `SMTP_HOST` |
| `smtp.port` | Outgoing mail port | Usually 587 for secure mail submission or 465 for implicit TLS. | `SUPYMEM_SMTP_PORT`, `SMTP_PORT` |
| `smtp.user` | Mailbox address | Your full email address, used to sign in and send mail. | `SUPYMEM_SMTP_USERNAME`, `SMTP_FROM`, `SMTP_USER`, `SMTP_USERNAME` |
| `smtp.password` | Mail app password | Create an app password in your mailbox account, rather than using your main password. | `SUPYMEM_SMTP_PASSWORD`, `SMTP_PASSWORD` |
| `smtp.sender_name` | Sender name | The name people see when you email them. | `SUPYMEM_SENDER_NAME`, `SMTP_SENDER_NAME`, `SENDER_NAME` |
| `smtp.postal_address` | Postal address | Your sender address stays in every outreach email for compliance. | `SUPYMEM_SENDER_POSTAL_ADDRESS`, `SMTP_POSTAL_ADDRESS`, `SENDER_POSTAL_ADDRESS` |
| `smtp.imap_host` | Incoming mail host | Your mailbox provider's IMAP server for reading replies. | `SUPYMEM_IMAP_HOST`, `IMAP_HOST` |
| `smtp.imap_port` | Incoming mail port | Usually 993 for secure incoming mail. | `SUPYMEM_IMAP_PORT`, `IMAP_PORT` |
| `smtp.booking_link` | Booking link | Your HTTPS scheduling page, included when you ask for a call. | `SUPYMEM_BOOKING_LINK`, `SMTP_BOOKING_LINK` |

<!-- connection: publishing -->
## Your page publishing account

Publish a page only when you ask. Your destination receives the public page, not your private workspace.

**Unlocks:** Publish a page built by your connected coding tool after it passes checks. Page measurement also needs a reachable collection endpoint; a saved account alone does not provide one. Connect your tool under **Coding tools** before requesting a new page.

Choose the destination you already use. Get a [Vercel token](https://vercel.com/account/settings/tokens) and project name, or a [Netlify token](https://app.netlify.com/user/applications) and site ID. For S3, use your [AWS account](https://console.aws.amazon.com/iam/) to obtain bucket-scoped credentials and an existing bucket, region, fresh page folder prefix, and reachable HTTPS site URL. Include a session token only for temporary credentials. Account credentials do not create an S3 website or HTTPS distribution.

Set **Waitlist email** to your signup inbox when using an email-based waitlist. Visitors open their mail app; that is not an automatically saved signup. Do not use your laptop's loopback address as a public site URL.

Save, choose **Test your connection**, and switch on **Publish pages** in **Settings > Capabilities**. Draft and preview the page before asking Charlie to publish. Local ZIP export needs no publishing account. Check the returned public URL and signup destination before sharing it.

**Data sent:** Publishing sends your public page files to the selected host. Waitlist links send a visitor's signup to your chosen address. Testing only checks your account or bucket.

| Saved field | In-app field | Purpose | `.env` variable names |
|---|---|---|---|
| `publishing.provider` | Publishing service | Choose the account that will host your public pages. | `SUPYMEM_PUBLISH_PROVIDER` |
| `publishing.project` | Project or site ID | Your Vercel project name or Netlify site ID. | `VERCEL_PROJECT_NAME`, `NETLIFY_SITE_ID` |
| `publishing.token` | Publishing token | Your Vercel or Netlify access token. | `VERCEL_TOKEN`, `NETLIFY_AUTH_TOKEN` |
| `publishing.bucket` | S3 bucket | The existing bucket where you want your public pages. | `SUPYMEM_S3_BUCKET`, `AWS_S3_BUCKET` |
| `publishing.region` | S3 region | The region of your bucket. | `AWS_REGION`, `AWS_DEFAULT_REGION` |
| `publishing.prefix` | S3 page folder | A folder prefix to separate published pages from other bucket files. | `SUPYMEM_S3_PREFIX`, `AWS_S3_PREFIX` |
| `publishing.site_url` | Public site URL | The HTTPS address visitors use to reach your S3-hosted pages. | `SUPYMEM_SITE_URL`, `SUPYMEM_PUBLIC_URL` |
| `publishing.access_key_id` | S3 access key ID | Your own access key with permission to publish to this bucket. | `AWS_ACCESS_KEY_ID` |
| `publishing.secret_access_key` | S3 secret access key | The secret paired with your access key ID. | `AWS_SECRET_ACCESS_KEY` |
| `publishing.session_token` | S3 session token | Include this only when you use temporary AWS credentials. | `AWS_SESSION_TOKEN` |
| `publishing.waitlist_email` | Waitlist email | Your inbox for visitor signups when you use an email-based waitlist. | `SUPYMEM_WAITLIST_EMAIL` |

## Coding tools

On Windows, builds need a trusted git.exe on `PATH`. Install Git for Windows in a folder only you or your administrator can change (such as `C:\Program Files\Git`) and add its `cmd` folder to `PATH`. Restart Supymem, then choose **Check connection** under **Settings > Keys and connections > Coding tools**. You do not need Git just to open the installed app.

In **Settings > Keys and connections > Coding tools**, follow **How to sign in** with your own subscription login. Choose **Check connection**, then **Allow Charlie to use** your tool. See [which tools support builds](../../README.md#build-with-your-own-coding-tool).

Charlie decides when a build is the right next step and offers it. When you ask, Charlie writes the brief, starts your tool, watches it, checks the result and shows it. Your tool builds your landing pages, product, fixes and small tools on your laptop. Supymem does not write your code or silently use another builder. If no tool is connected, Charlie offers this setup step.

Start with a static page or small local web app. Ask Charlie to split a larger product into smaller steps. Keep your app open, watch or stop work in **Build activity**, and choose **Open result folder** in your desktop app to see the files. Connecting a tool starts no work. Nothing is published unless you ask.

**Data sent:** Your coding tool receives the brief and the files needed for your requested work. Its selected service may process that information under your own account. Keep unrelated private files out of your build folder.

<!-- connection: search -->
## Your search and page reader

Built-in search and page reading work without a key. You can optionally use your own search account or Firecrawl.

**Unlocks:** Research, Find customers. Readiness also depends on the feature switch and its other required settings.

Keep **Search service** set to **Built-in** to search and read public pages without a key. This uses the internet. No Firecrawl installation is needed.

For an upgrade, get your key from [Tavily](https://app.tavily.com/) or [Exa](https://dashboard.exa.ai/), select that service, and enter **Search API key**. **OpenAI** search reuses your saved OpenAI model connection; it has no separate search key. Azure OpenAI search is not in this version: the local search adapter accepts OpenAI, not Azure. Keep Built-in selected for research with an Azure model. For **Firecrawl**, enter the URL of your own server and its token only if it requires authentication. Use HTTPS for a remote reader or HTTP for a loopback reader.

Save and choose **Test your connection**. Paid search services can charge for the test. **Research** is on by default in **Settings > Capabilities**. Keep page access limited to public URLs; imported page text cannot authorize work.

**Data sent:** Search sends your query to the selected search service. Page reading requests the URLs you ask to read. OpenAI search also sends your query to your model service.

| Saved field | In-app field | Purpose | `.env` variable names |
|---|---|---|---|
| `search.provider` | Search service | Built-in search needs no key. Other choices use your own account. | `SUPYMEM_SEARCH_PROVIDER` |
| `search.api_key` | Search API key | Your Tavily or Exa key. OpenAI uses your saved model key instead. | `TAVILY_API_KEY`, `EXA_API_KEY` |
| `search.firecrawl_url` | Firecrawl URL | Optional URL of your own Firecrawl server for reading or searching pages. | `FIRECRAWL_SELF_HOSTED_URL` |
| `search.firecrawl_key` | Firecrawl key | Only needed if your Firecrawl server requires authentication. | `FIRECRAWL_SELF_HOSTED_TOKEN`, `FIRECRAWL_API_KEY` |

<!-- connection: composio -->
## Your connected apps

Use your own Composio project to connect the apps you choose. Authorize each app on the Connections screen after saving.

**Unlocks:** Connected apps, Draft content. Readiness also depends on the feature switch and its other required settings.

Get a project API key and authorization configuration IDs from your [Composio Platform dashboard](https://dashboard.composio.dev/). **App authorization IDs** is a JSON object mapping each app's toolkit slug to its authorization configuration ID. Use the slugs and IDs from your project, not an account password or a consumer account key.

Save and choose **Test your connection**. Then switch on **Connected apps** in **Settings > Capabilities**, open **Connections**, select your app, and finish its consent flow. A project key alone does not authorize an app account. The test lists an authorization configuration without performing an app action. SMTP and IMAP do not require Composio.

**Data sent:** Composio receives app authorization details and the inputs to actions you request. Testing only lists one authorization configuration, without running an app action.

| Saved field | In-app field | Purpose | `.env` variable names |
|---|---|---|---|
| `composio.api_key` | Composio project key | Create a project key in your Composio account. | `COMPOSIO_API_KEY` |
| `composio.auth_configs` | App authorization IDs | Enter a JSON object mapping each app's toolkit slug to its Composio authorization configuration ID. | `SUPYMEM_COMPOSIO_AUTH_CONFIGS` |

<!-- connection: images -->
## Your image tools

Your content drafts work without image keys. Upload your own product screenshot or photo in Media Studio to add an image.

**Unlocks:** Draft content. These image settings are optional; your content feature still uses your saved model connection and feature switch.

Open **Settings > Keys and connections**, then **Your image tools** to save the service URL or keys you choose. Open **Media Studio** to upload your own image or request an image using your saved service. Saving these settings does not generate an image or contact a service.

**Data sent:** Your selected image service receives the image prompt or image-search terms and authentication it needs. A loopback image server keeps processing on your machine. Pixabay currently reports key availability without a stock-fetch action; fetching images through it is not in this version. Use your uploads or Pexels instead.

| Saved field | In-app field | Purpose | `.env` variable names |
|---|---|---|---|
| `images.comfyui_url` | ComfyUI URL | Use an image service you run yourself, or upload your own image in Media Studio. | `COMFYUI_URL` |
| `images.bfl_api_key` | BFL image key | Use your own BFL account when you request an image. | `BFL_API_KEY`, `BFL_API_TOKEN` |
| `images.gemini_api_key` | Gemini image key | Use your own Gemini account when you request an image. | `GEMINI_API_KEY`, `GOOGLE_API_KEY`, `GOOGLE_GENERATIVE_AI_API_KEY` |
| `images.pexels_api_key` | Pexels image key | Use your own Pexels account to find a stock image. | `PEXELS_API_KEY` |
| `images.pixabay_api_key` | Pixabay image key | Save your key for the availability check. Pixabay stock fetching is not in this version; use your uploads or Pexels. | `PIXABAY_API_KEY` |

<!-- connection: spend -->
## Your monthly spend cap

Your app checks this limit before paid calls. You can also set a limit with your model provider.

**Unlocks:** Background work, Run your experiments. Readiness also depends on the feature switch and its other required settings.

No account or key is needed. Enter your limit in USD, or leave it blank to remove your local limit. Zero blocks estimated paid calls. Save the setting before requesting paid work.

The app checks its recorded spending and estimated next call against this cap. It is not a provider billing guarantee: set a provider-side budget too, especially if you use the same account outside this app. If you reach the cap, return to this section to raise or remove it, or wait until the next month.

**Data sent:** Your limit stays on your machine. Your model provider receives only the requests you choose to run.

| Saved field | In-app field | Purpose | `.env` variable names |
|---|---|---|---|
| `spend.monthly_spend_cap` | Monthly limit (USD) | Leave blank to remove your local monthly limit. | `SUPYMEM_LOCAL_MONTHLY_CAP_USD` |

<!-- connection: data -->
## Your data folder and backup

Choose your backup folder. Stop your app before replacing your data folder. Backups contain encrypted saved settings, not the local encryption key file; preserve your recovery key separately.

**Unlocks:** Routines, Background work. Readiness also depends on the feature switch and its other required settings.

No account or key is needed. Choose an absolute backup folder path outside your data folder; leave it blank to use the default sibling backup folder. The current data folder is shown read-only on this screen. Changing the backup folder does not move existing backups or your databases.

Your desktop makes backups before updates. Full backups retain encrypted settings but exclude the local encryption key file; preserve that recovery key separately. Use **Your data and recovery** on this screen for **Export my data**, **Import my data**, and restoring a listed backup. Pause work before exporting; import and restore finish after the app restarts. Full backups contain private data and encrypted saved credentials; older vault files also contain encryption material. Protect the folder with disk encryption and private permissions. See [backups and security](backups-security.md).

**Data sent:** Your backups stay in the folder you choose. Your app does not upload them.

| Saved field | In-app field | Purpose | `.env` variable names |
|---|---|---|---|
| `data.backup_directory` | Full backup folder path | Choose an absolute path for your backups. Existing backups are not moved. | `SUPYMEM_BACKUP_DIRECTORY` |

## Known issues

A connection test or Ready badge does not show that the work finished. See [the customer search and scan checks and retry steps](../../README.md#known-issues).

Next, [create your first venture](first-venture.md). If a test fails, use the matching symptom in [troubleshooting](troubleshooting.md).
