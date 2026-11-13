# Avatar Store
Avatar Store is an online shopping marketplace from Odisha, with product discovery, seller listings, order tracking, and a storefront for everyday finds.

## Features
- Customer registration, login, editable profile, and order history.
- Ordered, category-grouped storefront sections, product variants, brand-aware search, price/category/brand/option filters, and product detail pages. Popular ordering uses completed sales, wishlist saves, and unique product visits.
- Guest and signed-in bags with quantity controls.
- Checkout with validated contact details, Indian postal-directory PIN/locality checks, COD, manual UPI verification, and configurable PayU; email verification is not required to place an order.
- Per-seller fulfillment tracking for placed, packed, dispatched, shipped, out-for-delivery, and delivered orders; customers see live-polled status and carrier updates.
- Customer cancellation reasons selected from a dropdown, plus downloadable PDF invoices after payment confirmation (or immediately for COD).
- Customer return/exchange requests within seven days of delivery; staff review, receipt/inspection, refund processing, manual reconciliation, and one-time stock restocking are managed in Django admin.
- Idempotent email notifications for order/payment/return events, with staff-visible delivery attempts and a management command to retry pending or failed messages.
- Optional inventory counts, low-stock admin filters, and price/cost snapshots on order lines.
- Staff-only revenue, estimated-profit, visitor-conversion, and low-stock dashboard.
- Dealer seller panel for store profiles, categorized product listings, inventory, product galleries, and order fulfillment. Each owner has private listing tabs and can update only shipments containing their own products. New listings and edits require at least three product photos. Active seller listings and their current prices, descriptions, offers, and categories appear in the public shop.
- Seller vault summary based on recorded paid and pending orders; payouts are not processed by the app.
- Dedicated seller portal with seller login and registration at `/seller-portal/`.
- Product galleries, categories, variants, customers, seller fulfillment, orders, and payment administration.
- Product detail pages with seller sale prices, automatically calculated percentage savings, descriptions, offers, and verified-purchase product ratings and reviews. Reviews are limited to customers with a delivered order and sellers cannot review their own listings.
- Private signed-in customer wishlists with save/remove controls on product cards and detail pages.
- Seller-managed product brands that shoppers can search and filter in the catalog.
- Signed-in customers can save, select, set a default for, and remove delivery addresses from checkout and Account.
- Seller-defined product coupons (up to three codes per listing), validated and applied to eligible items at checkout with discounts recorded on the order and payment.
- Checkout price breakdown with seller-configured product GST, configurable platform fee and fee GST, free delivery, and persisted order/payment totals.
- Installable progressive web app with a web app manifest, home-screen install prompt, cached static assets, and an offline notice.
- Persistent light/dark theme controls shared by the shopper and seller portals, with responsive layouts and system-theme detection.
- Search-friendly product pages with canonical/Open Graph metadata, Product JSON-LD, crawlable product sitemaps, and `robots.txt` rules for private routes.
- Downloadable, digitally generated order invoices with a configurable authorized-signatory name. This is a typed signatory on the PDF, not a certificate-based cryptographic PDF signature.
- Signed-in users can open seller settings from Account → **Be a seller**, publish or hide listings, update stock, and review orders containing their products. Seller listings require an active product category and appear in the storefront's category navigation.
- Delivery PIN entry and address collection at checkout. Postal checks validate the PIN, delivery status, city and state against India Post directory data; they do not prove a street address or phone ownership.
- Help and support page with contact details for avatarshop@gmail.com.

## Technologies
1. Django (Backend)
2. Bootstrap (Frontend)
3. CSS (Frontend)
4. JavaScript (Client-Side)
5. PayU (optional payment gateway; requires merchant configuration)

## Demonstration
<p align="left">
  <img width="780" src="preview/demonstration.gif">
</p>

## Preview
#### Store
<p align="left">
  <img width="780" src="preview/stores.png">
</p>

#### Cart
<p align="left">
  <img width="780" src="preview/cart.png">
</p>

#### Checkout
<p align="left">
  <img width="780" src="preview/check-out.png">
</p>

## To Run This Project
1. clone this project on your local machine,
```
git clone https://github.com/ruhulaminparvez/eCommerce.git
```
2. create a virtual environment inside eCommerce folder,
```
virtualenv venv
```
3. activate virtual environment,(for windows)
```
.\venv\Scripts\activate 
```
4. install project dependencies from requirements.txt,
```
pip install -r requirements.txt
```
5. apply migrations and run project on your local machine,
```
python manage.py migrate
python manage.py runserver
```

## Seller portal, authentication, and PostgreSQL

Open **Account → Seller login** or **Be a seller**, or visit `/seller-portal/`. The seller portal has its own login, signup, and dashboard layout using the store's theme. Seller and shopper accounts are shared. Signup activates the account and signs the user in immediately; username/email and password are enough for login. Guest checkout uses the supplied email for order communication without a verification step.

The project uses SQLite by default for local development. To use Neon/PostgreSQL, set `DATABASE_URL` in the process environment or in an untracked local `.env` file based on `.env.example`, install requirements, and run migrations:

```powershell
$env:DATABASE_URL = "postgresql://USER:PASSWORD@HOST/DB_NAME?sslmode=require"
python manage.py migrate
python manage.py runserver
```

Keep the database URL, `DJANGO_SECRET_KEY`, and mail credentials out of source control. Rotate any database credential that has been pasted into a chat, ticket, or public log before using it. The application does not automatically copy existing SQLite records into Neon; migrate the schema first, then import data separately if needed.

## SEO and public deployment

Product detail pages publish canonical URLs, share metadata, and Product structured data; `/sitemap.xml` lists active public products and `/robots.txt` excludes account, checkout, seller, and admin routes. Before production, set `SITE_URL` to the real HTTPS origin and configure `ALLOWED_HOSTS`, deploy with HTTPS, and submit `https://your-domain/sitemap.xml` in Google Search Console/Bing Webmaster Tools. Keep seller listings complete with accurate product titles, descriptions, prices, availability, brands, and photos. These are indexing foundations, not a guarantee of a first-place result; ranking depends on domain authority, competition, product content, reviews, performance, and search-engine decisions.

Use the seller dashboard or Django admin to edit product descriptions, offers, categories, variants, stock counts, and cost prices. Set each product's GST rate to the correct applicable slab; it defaults to 0% rather than guessing a tax rate. The platform fee defaults to 2%, with 18% GST on that fee; configure `PLATFORM_FEE_PERCENT` and `PLATFORM_FEE_GST_RATE` in the local environment to change them. Delivery is currently free. PayU requires merchant credentials to be set in the local environment before enabling online checkout:

```powershell
$env:PAYU_KEY = "your-test-or-live-key"
$env:PAYU_SALT = "your-test-or-live-salt"
$env:PAYU_PAYMENT_URL = "https://test.payu.in/_payment"
$env:AVATAR_UPI_ID = "your-merchant-upi-id"
python manage.py runserver
```

Use the live PayU endpoint only after configuring and testing a merchant account. PayU can offer cards and UPI apps such as Google Pay and PhonePe, but the exact instruments are controlled by the merchant account and enabled gateway configuration. Set `INVOICE_AUTHORIZED_SIGNATORY` to the name that should appear in the invoice signatory block; the generated PDF is not a cryptographically signed document or a substitute for configuring required GSTIN/legal invoice details. Never commit merchant secrets. Manual UPI opens the installed UPI app chooser; submitted references remain pending until staff verifies them. COD is also recorded as pending until staff records payment. Courier updates are not automatically sent to external providers; maintain carrier/status information in the order admin. Return requests are reviewed in Django admin: approve or reject the request, confirm receipt and inspection before processing a refund, and restock only after confirming returned goods. PayPal refunds are submitted through the configured API; PayU refund requests are queued and remain in processing until staff reconciles completion against PayU. Manual UPI and COD refunds require staff to process and verify the refund outside the app, then record the provider reference and explicitly reconcile it in admin. Configure `PAYU_REFUND_API_URL` for the matching test or live environment; the default is PayU's test refund endpoint. Products with a `NULL` stock count are not inventory-tracked until a stock count is entered. A listing with stock `0` is intentionally not purchasable until its owner restocks it. Profit excludes order lines without a recorded product cost.

PayPal Checkout is also available when configured with PayPal REST API credentials. Start with sandbox credentials and only switch to live credentials after testing a merchant account that is permitted to accept INR payments:

```powershell
$env:PAYPAL_CLIENT_ID = "your-paypal-client-id"
$env:PAYPAL_CLIENT_SECRET = "your-paypal-client-secret"
$env:PAYPAL_API_BASE = "https://api-m.sandbox.paypal.com"
python manage.py runserver
```

Use `https://api-m.paypal.com` only for a live account. PayPal must support INR for the merchant account; the checkout verifies the captured currency, amount, provider order reference, and completion status before marking the order paid. Keep credentials private and configure HTTPS for a deployed site. PayPal credentials are separate from PayU; PayPal is not enabled until both are set.

Email verification sends a one-time link and a 6-digit code; password resets use Django's one-time reset link. For real delivery outside local debug mode, configure SMTP. For Gmail, use an account with 2-Step Verification and a dedicated App Password (not the regular account password). Set these in the local environment before starting Django:

```powershell
$env:EMAIL_HOST = "smtp.gmail.com"
$env:EMAIL_PORT = "587"
$env:EMAIL_USE_TLS = "true"
$env:EMAIL_HOST_USER = "avatarshop@gmail.com"
$env:EMAIL_HOST_PASSWORD = "your-gmail-app-password"
$env:DEFAULT_FROM_EMAIL = "avatarshop@gmail.com"
```

Guest checkout requires a valid email address for order communication, but does not require an email verification link or code. Account email verification remains available separately. Order and return event emails use the same SMTP settings; failed/pending messages can be retried with `python manage.py retry_order_emails` (schedule this command with your deployment's task scheduler for ongoing retries). Delivery depends on working SMTP credentials and provider acceptance.

Phone OTP sign-in uses MSG91's OTP API (see [MSG91 OTP documentation](https://docs.msg91.com/otp)). The shopper must save a unique Indian mobile number in Account → profile before phone sign-in can be used. Create and approve an OTP template in MSG91, then configure the credentials locally:

```powershell
$env:MSG91_AUTH_KEY = "your-msg91-auth-key"
$env:MSG91_OTP_TEMPLATE_ID = "your-approved-otp-template-id"
python manage.py runserver
```

Never commit provider credentials. Phone OTP sign-in works for accounts with a saved Indian mobile number; the OTP is sent and verified by MSG91. SMS delivery and verification depend on an active MSG91 account, approved template, DLT compliance, account credits, and provider coverage. The install prompt is available on browsers that support PWA installation; production installation requires HTTPS (localhost is allowed for development), and iOS users can choose Add to Home Screen from Safari's Share menu. Product coupons are additional discounts against the seller's current selling price, not the crossed-out list price. A successful postal lookup confirms only postal-directory delivery-area data; it does not prove a street address exists or guarantee courier coverage.

## Deployed
👉 Live Demo: [Avatar Store](https://ruhulaminparvez.pythonanywhere.com/)

## Bugs
> *If there any known bugs found or run into any issues please let me know. Please enjoy and feel free to share your opinion, constructive criticism, or comments about my work. Email: 👉 ruhulaminparvez007@gmail.com 😊 Thank you!*
