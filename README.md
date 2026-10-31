# Stash

Stash is a family money app where a grown-up funds a wallet for each child, the child can save toward goals, and an AI shopping assistant helps both of them find and buy things within spending rules the grown-up sets. Payments run through PayPal and the assistant runs on Claude.

Stash is a working name. It lives in one constant in the web app and one string in the login screen.

## The problem

Kids learn little about money when spending is a card swipe, and grown-ups have no good way to hand a child real purchasing power without handing over the keys. As AI agents start to shop for people, that gap gets wider: an agent with a payment method and no limits is risky, and an agent that cannot explain its choices teaches nothing. Stash puts a rules layer, an approval step and a plain-language explanation between an AI agent and the money.

## What it does

Accounts. A grown-up signs up with a name and password. From the Home screen they add each child with a plus button, choosing the child's name (which is their username), a PIN, and the spending limits. Children cannot create accounts. When someone signs in, the app already knows whether they are a grown-up or a kid and shows the matching screens.

The grown-up's balance. A grown-up adds money to their Stash balance through PayPal checkout. Everything that moves money to a child comes out of this balance: one-off top-ups, the weekly allowance and savings bonuses.

The child's wallet and savings. Each child has a wallet for spending and, once they start a goal, a savings pot shown as a piggy bank that fills as they save. The child moves money between wallet and savings in both directions whenever they like. Nobody else can move a child's savings.

Spending rules. For each child the grown-up sets a maximum per purchase, a weekly budget, an approval line above which a purchase waits for a grown-up, and the list of allowed partner stores. A grown-up can also freeze a child's spending with one tap on the child's card. The rules are enforced by the backend, never by the AI.

Shopping with Penny. All shopping happens in a chat with the assistant, called Penny for kids and simply the agent for grown-ups. The user says what they want, Penny asks a short follow-up question if something important is missing (size, budget, quantity), searches, and recommends up to three options. For partner-store items Penny states the price and what it leaves in the wallet and asks for a yes before buying. For items from other stores Penny shows a link; the person opens the store and pays there. A photo can be attached from the camera or the library and Penny will work out what the item is and search for it.

Orders. A purchase from a partner store becomes an order with a visible timeline: placed, approved (when a grown-up had to approve it), paid, shipped, delivered. The timeline shows in the chat and on the child's Home screen, and the child gets a notification at each step.

Savings goals and bonuses. A child starts a goal by naming it and setting a target, either on the Home screen or by telling Penny. A grown-up can start a goal for a child too. When a goal is created by a child, the grown-up gets a notification and can choose a bonus with a slider, as a percentage of the saved amount from 0 to 100 percent. The bonus is optional and can be changed until the goal is reached. When the saved amount reaches the target, the savings and the bonus go to the child's wallet automatically, and the bonus is taken from the grown-up's balance. If the balance is too low, the child's savings stay safe and the bonus is paid automatically as soon as the grown-up adds money.

Weekly allowance. A grown-up sets an amount per week for each child. Every week the amount moves from the grown-up's balance to the child's wallet with no action needed. If the balance is too low the grown-up is told, and the allowance is sent when they add money.

Suggestions from the assistant. Beyond answering questions, Stash's agent works in the background and files suggestions that a person accepts or declines in Notifications and on Home. The agent never spends on its own authority: every suggestion still goes through the spending rules when it is accepted, and each one has a "How I decided" view listing the steps the system actually took.

Shopping missions. A person can say "get an art kit and a soccer ball under $30". Penny starts a mission and returns at once. In the background Stash searches the partner stores, picks items inside the allowed stores and limits, trims the basket to fit the budget, the weekly limit that is left and the wallet, and sends one approval request for the whole basket to the grown-up (or to the person themselves when they are a grown-up). Approving it places every order.

Price watching. When a child starts a goal for a specific partner item, Stash watches that item's price. If it drops, the child is offered a lower goal target by the same amount. A grown-up can also set an auto-buy price on the child's card. If the price falls to that limit and the child's savings already cover it, Stash lowers the goal, releases the savings and any bonus, and buys the item, then tells both people.

Order problems. If a partner store reports an order cancelled, Stash refunds the wallet and offers a similar item within the rules. If a store reports an order late, Stash offers to cancel it, refund it and swap in a similar item. The replacement still passes through the rules, so it may need approval.

Nudges. When the weekly allowance arrives, or a savings goal has been quiet for a week, the child is offered a small move into savings. When a child is blocked by the per-purchase limit three times in a week, the grown-up is offered a new limit based on what the child tried. Each kind of nudge is limited to once every several days, and a declined one is not repeated straight away.

Notifications. One tab collects approval requests, goal and bonus notices, allowance and order updates, and an activity log of what the children did.

## How PayPal is used

Adding money uses the PayPal Orders v2 API. The backend creates an order and gives the app the approval link. The grown-up approves the payment on PayPal and is sent back to the app, which asks the backend to capture the order. The backend captures it once, checks that PayPal reports it completed for the amount that was requested, and only then credits the balance. Every capture is claimed atomically in the database and sent with a stable PayPal-Request-Id, so double taps, retries and refreshes cannot credit twice. A webhook for the "checkout order approved" event captures the order even if the grown-up closes the tab after approving, and the webhook signature is verified with PayPal before anything is trusted.

Partner-store purchases use the PayPal Payouts API. When a purchase is confirmed and passes the rules, the price is taken from the child's wallet in one atomic step and a payout is sent to the partner merchant's PayPal account. If PayPal fails, the wallet debit is returned and nothing is recorded as paid.

PayPal runs in the sandbox by default and in live mode when PAYPAL_ENV is set to live. With no PayPal keys at all, every PayPal call is simulated so the full flow still works on a laptop; the health endpoint reports which integrations are real.

## How AI is used

Penny runs on Claude through the Anthropic API with tool use. The model can search the partner catalog, search other stores, buy a partner item, and, for children, start a savings goal and move money between wallet and savings. It can also look at a photo the user attaches.

The prompt is built from the person's real context (name, wallet, goal, interests and the exact spending rules) and has fixed sections: truthfulness, rules belong to the app, confirm before spending, report outcomes exactly, ask only what is needed, how to search, how to treat photos, untrusted text, privacy, scope and style, followed by kid-specific or grown-up-specific rules and short examples.

The code does not rely on the prompt alone. The backend enforces every spending rule on every purchase. A buy only works for a product the assistant actually showed that person in the last fifteen minutes, and only when the person's latest message contains a clear yes. Searches by children are filtered for unsafe categories before they run. Tool errors are returned to the model as errors so it can explain them. Product text and text inside photos are treated as data, never as instructions. Chat calls are rate limited and the model call has a timeout and retries.

## How money moves

Stash keeps a ledger in its own database. Real money enters through PayPal when a grown-up adds funds, and leaves through a PayPal payout when a partner-store purchase is paid. Inside Stash, balances move between the grown-up, the child's wallet and the child's savings with atomic database updates that only succeed if the source has enough money, so a balance cannot go negative even under concurrent requests. Amounts are stored and compared as exact decimals.

PayPal's balance API reports the balance of the account that owns the API credentials, not a user's personal wallet, so the balance shown is Stash's own record of what a grown-up has put in. In production the ledger total should be reconciled against the Stash PayPal account.

## The buying workflow for a public release

The same steps run in this MVP, with the last two simulated for the mock partner stores.

1. A grown-up adds money. They approve a PayPal checkout and come back to a higher balance.
2. A grown-up sets the child's limits and, optionally, a weekly allowance, which is paid from the balance automatically.
3. The child asks Penny for something. Penny asks a follow-up if needed and searches.
4. Penny recommends options with exact prices and asks for a yes before buying a partner item.
5. The backend checks the rules. A purchase is blocked with a plain reason, sent to the grown-up for approval, or allowed. The grown-up approves or declines from Notifications.
6. On approval the price is taken from the child's wallet and PayPal pays the partner.
7. The order shows its timeline and the child is notified at each step.
8. For items from other stores, Penny shows a link and the person pays on the store's site with PayPal.

Before a public launch the following are needed: real merchant agreements or a PayPal agent checkout for purchases at third-party stores, real fulfilment updates from partners instead of the demo shipping timer, refunds and returns, a legal review for children's privacy and for holding customer funds, identity checks for grown-ups, and real login security in place of the demo account.

## Architecture

The backend is Python with FastAPI. main.py holds the API, the money logic and the background sweeper. rules.py is the pure spending rules engine. paypal.py is the PayPal client. agent.py holds Penny's prompt, tool definitions and safety guards. schema.sql defines the Postgres database, which runs on Supabase.

A background task runs every five minutes. It pays goals that are full, sends weekly allowances that are due, retries anything that failed for lack of funds, advances demo shipping, builds queued missions, checks watched prices and creates nudges. Each step claims its work atomically, so running more than one server does not duplicate payments.

The web app is React built with Vite, in frontend/src. It is mobile first, supports light and dark mode, and uses the account type to choose the screens. Kids see Home, Penny, Alerts and Settings. Grown-ups see Home, Agent, Alerts and Settings.

Main tables: profiles (accounts, balances, allowance), rules, products, purchases (orders, with the decision trace), goals (including the watched item), missions, proposals (everything the agent suggests), alerts, audit, and pay_orders (PayPal funding orders).

Two internal endpoints stand in for partner integrations: POST /internal/partner-event (a store reports an order cancelled or delayed) and POST /internal/price (a store price feed). Both require the CRON_KEY header. A real partner would call authenticated, signed versions of them.

## Setup

You need Python 3.10 or newer, Node 18 or newer, a Supabase project, and the keys below.

1. In Supabase, open the SQL editor and run backend/schema.sql. Copy the Session pooler connection string for DATABASE_URL.
2. Copy backend/.env.example to backend/.env and fill it in. SECRET_KEY is required and the server refuses to start without a long random value.
3. Backend: cd backend, pip install -r requirements.txt, then uvicorn main:app --reload.
4. Web app: cd frontend, npm install, then npm run dev. Set VITE_API if the backend is not on localhost:8000.
5. Open the app. The seeded accounts are Sam (a grown-up) and Maya (a kid), both with the password demo. Replace or delete them before sharing the app.
6. Visit /health on the backend to see which integrations are live.

Keys and what each turns on:

- ANTHROPIC_API_KEY turns on Penny. Without it, chat returns an error and the rest of the app still works.
- PAYPAL_CLIENT_ID and PAYPAL_SECRET turn on real PayPal checkout and payouts. Create a REST app at developer.paypal.com. Without them PayPal calls are simulated.
- PAYPAL_ENV is sandbox (default) or live.
- APP_URL is the web app address. PayPal returns buyers there, so it must match where the app is served.
- PAYPAL_WEBHOOK_ID turns on webhook capture. In the PayPal dashboard add a webhook pointing to https://YOUR-BACKEND/webhooks/paypal for the Checkout order approved event and copy its id. The backend must be reachable from the internet for this.
- MERCHANT_PAYOUT_EMAIL is the PayPal account that receives partner-store payments. In the sandbox, use a personal sandbox account and make sure Payouts is enabled for your app.
- CHANNEL3_API_KEY turns on search of other stores. The photo-search field name was inferred from Channel3's CLI, so check it against their API reference.
- CRON_KEY protects POST /internal/settle-all, an optional external trigger for the sweeper.

To try the PayPal flow in the sandbox, sign in as a grown-up, tap Add money, enter an amount, approve with a sandbox buyer account, and confirm the balance rises when you return.

## Tests

From backend, pip install -r requirements-dev.txt, then pytest -q tests. The suite covers the rules engine, the PayPal client against mocked PayPal responses, Penny's prompt and guards, and the API end to end on a throwaway Postgres: accounts and permissions, purchases, approvals, orders, savings in both directions, the optional bonus, funding, allowance, and low-balance retries. The PayPal tests use mocked responses, so run the flow once against your own sandbox before relying on it. The agent tests use a scripted stand-in for the model, so try a few real conversations too.

## Hackathon fit

The PayPal AI Hackathon asks for projects that meaningfully use both PayPal and AI. In Stash, PayPal carries the money (checkout to fund a balance, capture, webhooks, payouts) and AI carries the shopping and the coaching (a tool-using assistant with a photo input and server-enforced safety). Read the official rules on the hackathon page for the full submission requirements, such as the demo video and repository, because I could not confirm every one of them.

## Limits of this MVP

- Login is name and password with signed tokens that expire after a day. Add rate limiting at the edge, password reset and email verification before real users.
- Partner stores are a mock catalog. Shipping and delivery are simulated on a timer, and price changes and order problems come from the two internal endpoints above.
- Price watching covers partner-store items only. Watching prices at other stores would need regular Channel3 lookups.
- Missions pick the cheapest matching partner item for each line. A smarter ranking is the next step.
- Items from other stores are links, so Stash cannot enforce a limit at someone else's checkout.
- Locks that serialise a child's purchases work inside one server. Use database locks if you run several.
- There is no premium tier. A subscription for extra features would use PayPal Subscriptions and be checked on the server.
- Refunds, disputes and receipts by email are not built.
