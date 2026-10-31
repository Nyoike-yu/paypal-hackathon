create table profiles(id serial primary key, name text, role text check (role in ('adult','child')), guardian_id int references profiles(id), balance numeric default 0, coaching boolean default true, frozen boolean default false);
create table rules(profile_id int primary key references profiles(id), per_purchase_cap numeric, weekly_budget numeric, approval_threshold numeric, allowed_stores text[]);
create table products(id serial primary key, store text, name text, price numeric, emoji text, category text);
create table purchases(id serial primary key, profile_id int references profiles(id), product_id int references products(id), amount numeric, status text, reason text, paypal_ref text, created_at timestamptz default now());
create table audit(id serial primary key, profile_id int references profiles(id), event text, detail text, created_at timestamptz default now());

insert into profiles(name,role,guardian_id,balance,coaching) values
('Sam','adult',null,120,false),('Maya','child',1,45,true);
insert into rules values (1,100,150,60,'{"Pixel Games","Toy Barn","Gadget Hub"}'),(2,25,40,15,'{"Pixel Games","Toy Barn"}');
insert into products(store,name,price,emoji,category) values
('Pixel Games','Space Racer',19.99,'🚀','games'),('Pixel Games','Block Builder',12.99,'🧱','games'),('Pixel Games','Mega Quest Bundle',39.99,'🎮','games'),
('Toy Barn','Soccer Ball',14.50,'⚽','sports'),('Toy Barn','Art Kit',9.99,'🎨','art'),('Toy Barn','Robot Dino',24.99,'🦖','toys'),
('Gadget Hub','Headphones',29.99,'🎧','tech'),('Gadget Hub','Smartwatch',79.99,'⌚','tech');

create table alerts(id serial primary key, profile_id int references profiles(id), title text, body text, next_steps text, created_at timestamptz default now());
alter table profiles add column secret text, add column interests text[] default '{}';
update profiles set secret='demo';

create index on purchases(profile_id, status, created_at desc);
create index on audit(profile_id, id desc);
create index on alerts(profile_id, id desc);
create index on profiles(guardian_id);
create unique index on profiles(lower(name));
create table goals(id serial primary key, child_id int references profiles(id), name text not null, target numeric not null, saved numeric not null default 0, bonus_pct numeric not null default 0, status text not null default 'active', created_by int references profiles(id), paid_bonus numeric, charge_failed boolean not null default false, created_at timestamptz default now(), paid_at timestamptz);
create unique index on goals(child_id) where status in ('active','paying');
create index on goals(status, saved);
alter table alerts add column goal_id int references goals(id);
insert into goals(child_id,name,target,saved,created_by) values (2,'Soccer ball',60,15,2);
alter table profiles add column allowance numeric not null default 0, add column allowance_next timestamptz, add column allowance_failed boolean not null default false;
create index on profiles(allowance_next) where allowance > 0;
alter table purchases add column fulfillment text, add column shipped_at timestamptz, add column delivered_at timestamptz;
create table pay_orders(order_id text primary key, profile_id int references profiles(id), amount numeric not null, status text not null default 'created', created_at timestamptz default now());
create index on pay_orders(profile_id, status);
create table proposals(id serial primary key, profile_id int references profiles(id), child_id int references profiles(id), kind text not null, title text not null, body text not null, data jsonb not null default '{}', trace jsonb not null default '[]', status text not null default 'open', created_at timestamptz default now(), decided_at timestamptz);
create index on proposals(profile_id, status);
create unique index on proposals(child_id, kind) where status='open' and kind in ('save','raise_limit','price_drop');
create table missions(id serial primary key, profile_id int references profiles(id), title text not null, budget numeric not null, items jsonb not null, deadline text, status text not null default 'queued', created_at timestamptz default now());
create index on missions(status, created_at);
alter table purchases add column trace jsonb;
alter table goals add column watch_product_id int references products(id), add column watch_price numeric, add column buy_below numeric, add column last_saved_at timestamptz;

-- Notifications: one inbox, real pushes, and a one-day life after something is dealt with.
-- (On an existing database, run from here down in the Supabase SQL editor.)
alter table alerts add column key text, add column needs_action boolean not null default false, add column act text, add column handled_at timestamptz;
create index on alerts(profile_id, handled_at);
create index on alerts(profile_id, key) where handled_at is null;
alter table purchases add column decided_at timestamptz;
create index on proposals(profile_id, decided_at);
create table push_subs(id serial primary key, profile_id int not null references profiles(id) on delete cascade, endpoint text not null unique, p256dh text not null, auth text not null, created_at timestamptz default now());
create index on push_subs(profile_id);

-- Refunds: money sent from a guardian's balance back to the PayPal account they paid with.
-- status: refunding (debited, waiting on PayPal) | refunded | failed (money given back in the app) | review (PayPal never confirmed: a person must check)
-- (On an existing database, run from here down in the Supabase SQL editor.)
create table refunds(id serial primary key, profile_id int not null references profiles(id), order_id text not null references pay_orders(order_id), capture_id text not null, amount numeric not null check (amount > 0), status text not null default 'refunding', paypal_ref text, reason text, attempts int not null default 1, tried_at timestamptz not null default now(), created_at timestamptz not null default now(), done_at timestamptz);
create index on refunds(order_id);
create index on refunds(status, tried_at) where status = 'refunding';
create unique index on proposals((data->>'order_id')) where kind = 'refund' and status = 'open';
