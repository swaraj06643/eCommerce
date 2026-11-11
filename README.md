# Avatar Store
Avatar is an eCommerce store created by Subhasiosh Rath in Odisha, with a welcoming animated brand intro and a storefront for everyday finds.

## Features
- Customer registration, login, editable profile, and order history.
- Product categories, variants, search, price/category/option filters, and product detail pages.
- Guest and signed-in bags with quantity controls.
- Checkout with email-link verification, Indian postal-directory PIN/locality checks, COD, manual UPI verification, and configurable PayU.
- Order status and carrier tracking details maintained by store staff; customer cancellation and return requests.
- Optional inventory counts, low-stock admin filters, and price/cost snapshots on order lines.
- Staff-only revenue, estimated-profit, visitor-conversion, and low-stock dashboard.
- Dealer seller panel for store profiles, product listings, inventory, and order summaries.
- Seller vault summary based on recorded paid and pending orders; payouts are not processed by the app.
- Dedicated seller portal with seller login and registration at `/seller-portal/`.
- Product, category, variant, customer, order, and payment administration.
- Product detail pages with descriptions, current prices, and admin-managed offers.
- Signed-in users can open seller settings from Account → **Be a seller**, publish or hide listings, update stock, and review orders containing their products.
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

Open **Account → Seller login** or **Be a seller**, or visit `/seller-portal/`. The seller portal has its own login, signup, and dashboard layout using the store's theme. Seller and shopper accounts are shared. Signup activates the account and signs the user in immediately; username/email and password are enough for login. Guest checkout can still request email confirmation for the guest-provided address.

The project uses SQLite by default for local development. To use Neon/PostgreSQL, set `DATABASE_URL` in the process environment or in an untracked local `.env` file based on `.env.example`, install requirements, and run migrations:

```powershell
$env:DATABASE_URL = "postgresql://USER:PASSWORD@HOST/DB_NAME?sslmode=require"
python manage.py migrate
python manage.py runserver
```

Keep the database URL, `DJANGO_SECRET_KEY`, and mail credentials out of source control. Rotate any database credential that has been pasted into a chat, ticket, or public log before using it. The application does not automatically copy existing SQLite records into Neon; migrate the schema first, then import data separately if needed.

Use Django admin to edit product descriptions, offers, categories, variants, stock counts, and cost prices. PayU requires merchant credentials to be set in the local environment before enabling online checkout:

```powershell
$env:PAYU_KEY = "your-test-or-live-key"
$env:PAYU_SALT = "your-test-or-live-salt"
$env:PAYU_PAYMENT_URL = "https://test.payu.in/_payment"
$env:AVATAR_UPI_ID = "your-merchant-upi-id"
python manage.py runserver
```

Use the live PayU endpoint only after configuring and testing a merchant account. Never commit merchant secrets. Manual UPI references and COD payments remain pending until staff verifies/records payment in admin. PayU refunds and courier updates are not automatically sent to external providers; process refunds in PayU and maintain carrier/status information in the order admin. Products with a `NULL` stock count are not inventory-tracked until a stock count is entered. Profit excludes order lines without a recorded product cost.

Email verification sends a one-time link and a 6-digit code; password resets use Django's one-time reset link. For real delivery outside local debug mode, configure SMTP. For Gmail, use an account with 2-Step Verification and a dedicated App Password (not the regular account password). Set these in the local environment before starting Django:

```powershell
$env:EMAIL_HOST = "smtp.gmail.com"
$env:EMAIL_PORT = "587"
$env:EMAIL_USE_TLS = "true"
$env:EMAIL_HOST_USER = "avatarshop@gmail.com"
$env:EMAIL_HOST_PASSWORD = "your-gmail-app-password"
$env:DEFAULT_FROM_EMAIL = "avatarshop@gmail.com"
```

Checkout stays blocked until the customer opens the one-hour verification link or confirms the matching email code.

Phone OTP sign-in uses MSG91's OTP API (see [MSG91 OTP documentation](https://docs.msg91.com/otp)). Create and approve an OTP template in MSG91, then configure the credentials locally:

```powershell
$env:MSG91_AUTH_KEY = "your-msg91-auth-key"
$env:MSG91_OTP_TEMPLATE_ID = "your-approved-otp-template-id"
python manage.py runserver
```

Never commit provider credentials. Phone OTP sign-in works for accounts with a saved Indian mobile number; the OTP is sent and verified by MSG91. SMS delivery and verification depend on an active MSG91 account, approved template, DLT compliance, account credits, and provider coverage. A successful postal lookup confirms only postal-directory delivery-area data; it does not prove a street address exists or guarantee courier coverage.

## Deployed
👉 Live Demo: [Avatar Store](https://ruhulaminparvez.pythonanywhere.com/)

## Bugs
> *If there any known bugs found or run into any issues please let me know. Please enjoy and feel free to share your opinion, constructive criticism, or comments about my work. Email: 👉 ruhulaminparvez007@gmail.com 😊 Thank you!*
