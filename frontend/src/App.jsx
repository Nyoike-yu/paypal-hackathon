import { useEffect, useRef, useState } from "react";
const API = import.meta.env.VITE_API || "http://localhost:8000";
const APP = "Stash"; // working name: change it here
const hdr = () => ({ "Content-Type": "application/json", Authorization: `Bearer ${localStorage.getItem("t") || ""}` });
const request = async (path, body) => {
  try {
    const res = await fetch(API + path, { method: body !== undefined ? "POST" : "GET", headers: hdr(), body: body !== undefined ? JSON.stringify(body) : undefined });
    if (res.status === 401 && localStorage.getItem("t")) { localStorage.clear(); location.reload(); }
    return { status: res.status, data: await res.json().catch(() => ({ detail: "Something went wrong. Try again." })) };
  } catch { return { status: 0, data: { detail: "Can't reach the server." } }; }
};
const call = async (path, body) => (await request(path, body)).data;
// ---- notifications: standard Web Push, so it works on phones and computers ----
const b64u = k => Uint8Array.from(atob((k + "=".repeat((4 - (k.length % 4)) % 4)).replace(/-/g, "+").replace(/_/g, "/")), c => c.charCodeAt(0));
const ios = () => /iPad|iPhone|iPod/.test(navigator.userAgent);
const swReady = () => Promise.race([navigator.serviceWorker.ready, new Promise((_, no) => setTimeout(no, 4000))]);
const canPush = () => "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
const mySub = async () => (await swReady()).pushManager.getSubscription();
const tell = async sub => { const j = sub.toJSON(); return call("/push/subscribe", { endpoint: j.endpoint, p256dh: j.keys.p256dh, auth: j.keys.auth }); };
async function pushState() {
  if (!canPush()) return ios() ? "ios" : "unsupported";
  if (Notification.permission === "denied") return "blocked";
  try {
    if (!(await call("/push/key")).enabled) return "server";
    return Notification.permission === "granted" && (await mySub()) ? "on" : "off";
  } catch { return "unsupported"; }
}
async function pushOn() {  // must run from a tap: browsers only ask permission after one
  try {
    const k = await call("/push/key"); if (!k.enabled) return "server";
    if ((await Notification.requestPermission()) !== "granted") return "blocked";
    const reg = await swReady();
    const sub = (await reg.pushManager.getSubscription()) || (await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64u(k.key) }));
    return (await tell(sub)).detail ? "error" : "on";
  } catch { return "error"; }
}
async function pushOff() {
  try { const sub = await mySub(); if (sub) { await call("/push/unsubscribe", { endpoint: sub.endpoint }); await sub.unsubscribe(); } } catch {}
}
// where a tapped notification should land: /?go=Alerts&item=p12 or /?do=funds
const parseIntent = u => { const q = new URL(u, location.origin).searchParams; return q.get("go") || q.get("do") ? { go: q.get("go"), do: q.get("do"), item: q.get("item") } : null; };
const TABS = ["Home", "Penny", "Agent", "Alerts", "Settings"];
const shrink = async f => {
  const bmp = await createImageBitmap(f), k = Math.min(1, 1024 / Math.max(bmp.width, bmp.height)), c = document.createElement("canvas");
  c.width = Math.round(bmp.width * k); c.height = Math.round(bmp.height * k);
  c.getContext("2d").drawImage(bmp, 0, 0, c.width, c.height);
  return c.toDataURL("image/jpeg", 0.8).split(",")[1];
};
const money = n => `$${Number(n).toFixed(2)}`;
const msg = d => (typeof d === "string" ? d : "Check the fields and try again.");
const greet = () => { const h = new Date().getHours(); return h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening"; };
const nextDate = d => new Date(d).toLocaleDateString(undefined, { weekday: "long", month: "short", day: "numeric" });
const Logo = () => <div className="logo"><Piggy deco pct={55} mood="happy" /></div>;
const LBL = { Agent: "Penny" };  // one name per tab: the adult chat is Penny too
const Skel = ({ h }) => <div className="skel" style={{ height: h }} />;
const Empty = ({ text, mood = "happy" }) => <div className="empty"><Piggy small deco pct={40} mood={mood} /><p className="mute">{text}</p></div>;
// product photo: the server can send image, image_url or images[]; partner items fall back to their emoji, then the first letter
const imgOf = o => o.image || o.image_url || (Array.isArray(o.images) && o.images[0]) || null;
function Pick({ o, open }) {
  const [bad, setBad] = useState(false), src = imgOf(o), ok = o.verdict === "ok", label = { ok: "Within limits", needs_approval: "Needs approval" }[o.verdict] || "Blocked";
  if (o.link_only) return (  // a backup link to a store's own search page: no price, no rules check
    <div className="pick2">
      <div className="shot"><span aria-hidden="true">{o.store[0]}</span></div>
      <b className="pt">{o.store}</b><span className="mute">Search there</span>
      <button className="alt wide" onClick={() => open(o)}>Open</button>
    </div>
  );
  return (
    <div className="pick2">
      <div className={"shot" + (ok ? "" : " dim")}>{src && !bad ? <img src={src} alt="" loading="lazy" onError={() => setBad(true)} /> : <span aria-hidden="true">{o.emoji || (o.title || "?")[0]}</span>}</div>
      <b className="pt">{o.title}</b><span className="mute">{o.store}</span><p className="price">{money(o.price)}</p>
      <span className={`state ${ok ? "ok" : "no"}`}><Svg n={ok ? "check" : "alert"} s={14} />{label}</span>
      {o.checks?.length > 0 && <details open={!ok}><summary className="link">Rules check</summary><ul className="check">{o.checks.map((c, i) => <li key={i} className={c.level === "ok" ? "good" : "warn"}><Svg n={c.level === "ok" ? "check" : "alert"} s={16} /><span>{c.text}</span></li>)}</ul></details>}
      <button className="alt wide" onClick={() => open(o)}>Open</button>
    </div>
  );
}
const moodOf = m => (m.err || (m.picks?.length && m.picks.every(o => o.verdict === "blocked")) || m.order?.status === "blocked" ? "sorry" : m.picks?.length || m.order ? "happy" : "idle");
const LABEL = { bought: "Bought", completed: "Bought", blocked: "Blocked", pending: "Needs approval", approved: "Approved", declined: "Declined", rules: "Rules changed", goal: "Goal", saved: "Saved", paid: "Goal reached", allowance: "Allowance", funds: "Money added", freeze: "Freeze changed", topup: "Money added", reward: "Reward", link: "Opened a link", refused: "Stopped" };
const PIG = "M3.5 13a7.5 5.5 0 1015 0a7.5 5.5 0 10-15 0zM18.5 11.5h2.5v3h-2.5M13.5 8L15 5l2 4.5M7 17.6v3M14 18v2.6M8.5 9.2h3M15.8 12.2v.01";  // piggy bank: body, snout, ear, legs, coin slot, eye
const P = {
  Home: "M3 11l9-8 9 8v10h-6v-6H9v6H3z", Penny: PIG, Agent: PIG,
  Alerts: "M6 17v-6a6 6 0 0112 0v6l2 2H4z", Rules: "M4 7h10M18 7h2M4 17h2M10 17h10M14 5v4M6 15v4", Settings: "M12 12a4 4 0 100-8 4 4 0 000 8zM4 21a8 8 0 0116 0",
  plus: "M12 5v14M5 12h14", media: "M4 5h16v14H4zM9 10a1.5 1.5 0 100-3 1.5 1.5 0 000 3zM4 17l5-5 4 4 3-3 4 4", send: "M4 12l16-8-6 16-3-7z", chev: "M6 9l6 6 6-6", close: "M6 6l12 12M18 6L6 18", check: "M5 12l5 5 9-10", alert: "M12 7v6M12 17v.01",
  snow: "M12 2v20M4.9 7l14.2 10M19.1 7L4.9 17M9 4l3 2 3-2M9 20l3-2 3 2",
};
const Svg = ({ n, s = 24 }) => <svg viewBox="0 0 24 24" width={s} height={s} fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={P[n]} /></svg>;
const Bar = ({ a, b }) => <div className="bar" role="progressbar" aria-valuenow={Math.min(100, Math.round((a / b) * 100))} aria-valuemin="0" aria-valuemax="100"><i style={{ width: Math.min(100, (a / b) * 100) + "%" }} /></div>;
function Piggy({ pct = 0, small, tiny, deco, mood = "idle" }) {  // mood: idle, thinking, happy, sorry
  const drop = ((100 - pct) / 100) * 92, ink = { fill: "none", stroke: "var(--blue)", strokeWidth: 4, strokeLinecap: "round" };
  const eye = mood === "happy" ? <path d="M132 76q6-8 12 0" {...ink} /> : <circle cx="138" cy="74" r="4" fill="var(--blue)" />;
  return (
    <svg className={"piggy" + (small ? " small" : "") + (tiny ? " tiny" : "")} viewBox="0 0 200 160" {...(deco ? { "aria-hidden": true } : { role: "img", "aria-label": `Piggy bank, ${pct}% full` })}>
      <defs><clipPath id="pig-body"><ellipse cx="100" cy="88" rx="62" ry="46" /></clipPath></defs>
      <g fill="var(--card)" stroke="var(--blue)" strokeWidth="5" strokeLinejoin="round"><rect x="62" y="118" width="18" height="26" rx="7" /><rect x="122" y="118" width="18" height="26" rx="7" /><path d="M116 48l16-20 8 24z" /><ellipse cx="100" cy="88" rx="62" ry="46" /><rect x="150" y="76" width="28" height="28" rx="11" /></g>
      <g clipPath="url(#pig-body)"><rect className="piggy-fill" x="38" y="42" width="124" height="92" style={{ transform: `translateY(${drop}px)` }} /></g>
      <ellipse cx="100" cy="88" rx="62" ry="46" fill="none" stroke="var(--blue)" strokeWidth="5" />
      <path d="M38 78c-12-2-18-12-12-22M82 44h28" fill="none" stroke="var(--blue)" strokeWidth="5" strokeLinecap="round" />
      {eye}<circle cx="160" cy="90" r="2.5" fill="var(--blue)" /><circle cx="168" cy="90" r="2.5" fill="var(--blue)" />{mood === "thinking" && <g fill="var(--blue)"><circle cx="150" cy="40" r="4" /><circle cx="164" cy="30" r="5" /><circle cx="182" cy="18" r="6" /></g>}{mood === "sorry" && <path d="M130 62l14 5M146 114q8-7 16 0" {...ink} />}
    </svg>
  );
}

export default function App() {
  const [me, setMe] = useState(Number(localStorage.getItem("me")) || null), [info, setInfo] = useState(null), [tab, setTab] = useState("Home"), [toast, setToast] = useState(null);
  const timer = useRef();
  const refresh = () => me && call(`/profiles/${me}`).then(r => (r.profile ? setInfo(r) : (localStorage.clear(), setMe(null))));
  const say = (text, action) => { setToast({ text, action }); clearTimeout(timer.current); timer.current = setTimeout(() => setToast(null), 4500); };
  const [tick, setTick] = useState(0), [intent, setIntent] = useState(() => {  // a tapped notification opens the app at its address
    const i = parseIntent(location.href); if (!i) return null;
    const u = new URLSearchParams(location.search); ["go", "do", "item"].forEach(k => u.delete(k));
    history.replaceState({}, "", location.pathname + (u.toString() ? "?" + u : "")); return i;
  });
  const logout = async () => { await pushOff(); localStorage.clear(); setMe(null); setInfo(null); };  // this device stops getting this person's notifications
  useEffect(() => { refresh(); setTab("Home"); }, [me]);
  useEffect(() => {
    if (!me || !canPush()) return;
    const onMsg = e => { if (e.data?.type === "push") { setTick(t => t + 1); refresh(); } if (e.data?.type === "open") setIntent(parseIntent(e.data.url)); };
    navigator.serviceWorker.addEventListener("message", onMsg);
    if (Notification.permission === "granted") mySub().then(sub => sub && tell(sub)).catch(() => {});  // keep this device tied to whoever is signed in
    return () => navigator.serviceWorker.removeEventListener("message", onMsg);
  }, [me]);
  useEffect(() => {  // open the screen a notification points at
    if (!intent || !info) return;
    const t = intent.go === "Chat" ? (info.profile.role === "child" ? "Penny" : "Agent") : intent.go;
    if (TABS.includes(t)) setTab(t);
    if (!intent.item && (intent.do !== "funds" || info.profile.role === "child")) setIntent(null);
  }, [intent, info]);
  useEffect(() => {  // PayPal sends the buyer back here after the checkout
    if (!me) return;
    const u = new URLSearchParams(location.search), flow = u.get("paypal");
    if (!flow) return;
    history.replaceState({}, "", location.pathname);
    if (flow === "cancel") { say("Payment cancelled. Nothing was charged."); return; }
    const oid = u.get("token"); if (!oid) return;
    call("/funds/capture", { order_id: oid }).then(r => { say(r.detail ? msg(r.detail) : "Money added to your balance"); refresh(); });
  }, [me]);
  if (!me) return <Login onIn={r => { localStorage.setItem("t", r.token); localStorage.setItem("me", r.id); setMe(r.id); }} />;
  if (!info) return <div className="app stack" aria-busy="true" aria-label="Loading"><Skel h={56} /><Skel h={150} /><Skel h={110} /></div>;
  const p = info.profile, kid = p.role === "child";
  const tabs = kid ? ["Home", "Penny", "Alerts", "Settings"] : ["Home", "Agent", "Alerts", "Settings"];
  const V = { Home: kid ? KidHome : AdultHome, Penny: Chat, Agent: Chat, Alerts, Settings }[tab];
  const go = t => { setTab(t); setToast(null); };
  return (
    <div className={`app${kid ? " kid" : ""}${tab === "Penny" || tab === "Agent" ? " chat" : ""}`}>
      {tab === "Home" && <header><h1>{greet()}, {p.name}</h1></header>}
      <main>{tab === "Home" && <NotifyPrompt say={say} kid={kid} />}<V me={me} info={info} refresh={refresh} say={say} go={go} logout={logout} tick={tick} intent={intent} done={() => setIntent(null)} nav={setIntent} /></main>
      <div className="toast" role="status" aria-live="polite">{toast && <div><span>{toast.text}</span>{toast.action && <button onClick={() => go(toast.action)}>See why</button>}</div>}</div>
      <nav className="tabs" aria-label="Main">{tabs.map(t => <button key={t} className={t === tab ? "on" : ""} aria-current={t === tab ? "page" : undefined} onClick={() => go(t)}><Svg n={t} />{LBL[t] || t}</button>)}</nav>
    </div>
  );
}

function Login({ onIn }) {
  const [reg, setReg] = useState(false), [name, setName] = useState(""), [secret, setSecret] = useState(""), [err, setErr] = useState("");
  const go = async () => { setErr(""); const r = await call(reg ? "/register" : "/login", { name, secret }); r.detail ? setErr(msg(r.detail)) : onIn(r); };
  return (
    <div className="app stack">
      <div className="brand"><Logo /><h1>{APP}</h1></div>
      <label>Name<input value={name} autoComplete="username" onChange={e => setName(e.target.value)} /></label>
      <label>Password or PIN<input type="password" value={secret} autoComplete="current-password" onChange={e => setSecret(e.target.value)} onKeyDown={e => e.key === "Enter" && go()} /></label>
      {err && <p className="err" role="alert">{err}</p>}
      <button className="btn wide" disabled={!name || !secret} onClick={go}>{reg ? "Create account" : "Sign in"}</button>
      <button className="link wide" onClick={() => { setReg(!reg); setErr(""); }}>{reg ? "Back to sign in" : "New grown-up account"}</button>
      <p className="mute center">Kids get their profile from a grown-up.</p>
    </div>
  );
}

function Timeline({ o }) {
  const declined = o.status === "declined", cancelled = o.status === "cancelled", needsOk = !cancelled && (o.was_approved || o.status === "pending" || declined), steps = [["Placed", true]];
  if (needsOk) steps.push([declined ? "Declined" : "Approved", o.status !== "pending"]);
  if (cancelled) steps.push(["Cancelled", true]);
  else if (!declined) steps.push(["Paid", o.status === "completed"], ["Shipped", ["shipped", "delivered"].includes(o.fulfillment)], ["Delivered", o.fulfillment === "delivered"]);
  const next = steps.findIndex(s => !s[1]);
  return <ol className="timeline" aria-label="Order progress">{steps.map(([l, done], i) => <li key={l} className={done ? "done" : i === next ? "now" : ""}>{l}</li>)}</ol>;
}

const BAD = /over|block|not allowed|exceed|above|approv|ask|too |can't|cannot|paused|frozen/i;  // rough read of each rule line
function Trace({ steps, label, open }) {
  if (!steps || !steps.length) return null;
  return <details open={open}><summary className="link">{label}</summary><ul className="check">{steps.map((t, i) => { const bad = BAD.test(t); return <li key={i} className={bad ? "warn" : "good"}><Svg n={bad ? "alert" : "check"} s={16} /><span>{t}</span></li>; })}</ul></details>;
}

const YES = { mission: "Approve basket", replace: "Swap it", price_drop: "Lower my goal", save: "Move it", raise_limit: "Raise it", refund: "Send refund" };
const answer = async (p, yes, say) => {
  const r = await call(`/proposals/${p.id}/${yes ? "accept" : "decline"}`, {});
  say(r.detail ? msg(r.detail) : !yes ? "Okay, skipped" : r.refund ? r.message : r.results ? `${r.results.filter(x => x.status === "completed").length} of ${r.results.length} items ordered` : r.result === "pending" ? "Sent to a grown-up to approve" : "Done");
};
function Proposals({ me, refresh, say, limit, tick }) {
  const [rows, setRows] = useState([]);
  const load = () => call("/proposals").then(x => setRows(Array.isArray(x) ? x : []));
  useEffect(() => { load(); const t = setInterval(load, 20000); return () => clearInterval(t); }, [me, tick]);
  const act = async (p, yes) => { await answer(p, yes, say); load(); refresh(); };
  return <>{rows.slice(0, limit || 20).map(p => (
    <div className="card stack suggest" key={p.id}><b>{p.title}</b><p style={{ margin: 0 }}>{p.body}</p><Trace steps={p.trace} label="How I decided" />
      <div className="row"><button className="btn" onClick={() => act(p, true)}>{YES[p.kind] || "Yes"}</button><button className="alt" onClick={() => act(p, false)}>No thanks</button></div></div>))}</>;
}

function WatchEditor({ goal, say, onSaved }) {
  const [v, setV] = useState(goal.buy_below ? String(goal.buy_below) : "");
  const save = async () => { const r = await call(`/goals/${goal.id}/watch`, { buy_below: Number(v) > 0 ? Number(v) : null }); say(r.detail ? msg(r.detail) : Number(v) > 0 ? "Auto-buy set" : "Auto-buy off"); if (!r.detail) onSaved(); };
  return (
    <div className="stack">
      <div className="spread"><b>Watching {goal.watch_name}</b><span className="mute">now {money(goal.watch_price)}</span></div>
      <label>Buy it automatically if the price drops to ($)<input type="number" inputMode="decimal" step="0.01" value={v} placeholder="Off" onChange={e => setV(e.target.value)} /></label>
      <p className="mute">Uses their savings, and only if they already have enough. Leave it empty to just get a heads-up.</p>
      <button className="alt wide" onClick={save}>Save</button>
    </div>
  );
}

function OrderCard({ o: first, onDone }) {
  const [o, setO] = useState(first), [ask, setAsk] = useState(false), [busy, setBusy] = useState(false), [note, setNote] = useState("");
  useEffect(() => setO(first), [first]);  // newer data from the list replaces what this card shows
  const canCancel = o.status === "pending" || (o.status === "completed" && ["processing", "shipped"].includes(o.fulfillment));  // the server checks again
  const cancel = async () => {
    setBusy(true); const r = await call(`/orders/${o.id}/cancel`, {}); setBusy(false); setAsk(false);
    if (r.order) { setO(r.order); setNote(r.note); onDone && onDone(); } else setNote(msg(r.detail));
  };
  return (
    <div className="ordercard"><div className="spread"><b>{o.item}</b><span className="chip">{money(o.amount)}</span></div><p className="mute">{o.store}</p><Timeline o={o} />
      {canCancel && !ask && <button className="link" onClick={() => setAsk(true)}>Cancel order</button>}
      {canCancel && ask && <div className="stack"><p>{o.status === "pending" ? "Withdraw this request? Nothing was charged." : `Cancel this order? ${money(o.amount)} goes back to your wallet.`}</p><div className="row"><button className="alt" disabled={busy} onClick={cancel}>Yes, cancel</button><button className="link" onClick={() => setAsk(false)}>Keep it</button></div></div>}
      {note && <p className="mute" role="status">{note}</p>}
      <Trace steps={o.trace} label="How Penny decided" open={["blocked", "pending"].includes(o.status)} /></div>
  );
}

function Orders({ me, tick, refresh }) {
  const [rows, setRows] = useState([]);
  useEffect(() => { const load = () => call(`/orders/${me}`).then(x => setRows(Array.isArray(x) ? x : [])); load(); const t = setInterval(load, 30000); return () => clearInterval(t); }, [me, tick]);
  if (!rows.length) return null;
  return <section className="card stack"><h2 style={{ margin: 0 }}>Orders</h2>{rows.slice(0, 3).map(o => <OrderCard key={o.id} o={o} onDone={refresh} />)}</section>;
}

function Sheet({ title, onClose, children }) {
  useEffect(() => { const k = e => e.key === "Escape" && onClose(); window.addEventListener("keydown", k); return () => window.removeEventListener("keydown", k); }, []);
  return <div className="modal" role="dialog" aria-modal="true" aria-label={title} onClick={onClose}><div className="card stack" onClick={e => e.stopPropagation()}><h2>{title}</h2>{children}</div></div>;
}

function BonusSlider({ goalId, pct, target, say, onSaved }) {
  const [v, setV] = useState(pct);
  useEffect(() => setV(pct), [pct]);
  const save = async () => { const r = await call(`/goals/${goalId}/bonus`, { percent: v }); say(r.detail ? msg(r.detail) : v ? `Bonus set to ${v}%` : "Bonus removed"); if (!r.detail) onSaved(); };
  return (
    <div className="stack">
      <label>Bonus when they reach it: <b>{v}%</b> = {money((target * v) / 100)}
        <input type="range" min="0" max="100" step="5" value={v} onChange={e => setV(Number(e.target.value))} aria-valuetext={`${v} percent, ${money((target * v) / 100)}`} /></label>
      <p className="mute">Optional. Whatever you can add helps them stay motivated.</p>
      {v !== pct && <button className="btn wide" onClick={save}>Save bonus</button>}
    </div>
  );
}

function MoveSheet({ me, p, g, start, onClose, refresh, say, onCoin }) {
  const [dir, setDir] = useState(start), [amt, setAmt] = useState(""), [err, setErr] = useState("");
  const need = g.target - g.saved, max = Math.max(0, Math.floor((dir === "in" ? Math.min(need, p.balance) : g.saved) * 100) / 100);
  const go = async () => {
    const r = await call(dir === "in" ? "/savings" : "/savings/withdraw", { profile_id: me, amount: Number(amt), goal_id: g.id });
    if (r.detail) return setErr(msg(r.detail));
    onClose(); refresh(); if (dir === "in") onCoin?.();
    say(dir === "out" ? `${money(Number(amt))} is back in your wallet` : r.paid ? "Goal reached! Your savings and bonus are in your wallet." : r.reached ? "Goal reached! Waiting for the bonus." : `${money(Number(amt))} moved to savings`);
  };
  return (
    <Sheet title="Move money" onClose={onClose}>
      <div className="seg" role="group" aria-label="Direction"><button className={dir === "in" ? "on" : ""} onClick={() => { setDir("in"); setErr(""); }}>To savings</button><button className={dir === "out" ? "on" : ""} onClick={() => { setDir("out"); setErr(""); }}>To wallet</button></div>
      <p className="mute">{dir === "in" ? `Wallet ${money(p.balance)}. ${money(need)} to go for ${g.name}.` : `Savings for ${g.name}: ${money(g.saved)}.`}</p>
      <label>Amount ($)<input autoFocus type="number" inputMode="decimal" step="0.01" value={amt} onChange={e => setAmt(e.target.value)} /></label>
      <button className="link" onClick={() => setAmt(String(max))}>Use the most ({money(max)})</button>
      {err && <p className="err" role="alert">{err}</p>}
      <button className="btn wide" disabled={!(Number(amt) > 0)} onClick={go}>{dir === "in" ? "Move to savings" : "Move to wallet"}</button>
    </Sheet>
  );
}

function FundsSheet({ onClose }) {
  const [amt, setAmt] = useState(""), [err, setErr] = useState(""), [busy, setBusy] = useState(false);
  const go = async () => {
    setBusy(true); const r = await call("/funds/create", { amount: Number(amt) });
    if (r.detail) { setBusy(false); return setErr(msg(r.detail)); }
    window.location.assign(r.approve_url);  // PayPal checkout, then back here
  };
  return (
    <Sheet title="Add money" onClose={onClose}>
      <p className="mute">You'll approve the payment on PayPal and come straight back. Allowances, top-ups and bonuses come out of this balance.</p>
      <label>Amount ($)<input autoFocus type="number" inputMode="decimal" step="0.01" value={amt} onChange={e => setAmt(e.target.value)} /></label>
      {err && <p className="err" role="alert">{err}</p>}
      <button className="btn wide" disabled={!(Number(amt) > 0) || busy} onClick={go}>{busy ? "Opening PayPal…" : "Continue to PayPal"}</button>
    </Sheet>
  );
}

function GoalSheet({ me, onClose, onMade, refresh, say }) {
  const [name, setName] = useState(""), [target, setTarget] = useState(""), [err, setErr] = useState("");
  const go = async () => { const r = await call("/goals", { child_id: me, name: name.trim(), target: Number(target) }); if (r.detail) return setErr(msg(r.detail)); onClose(); onMade?.(r.id); refresh(); say("Goal started"); };
  return (
    <Sheet title="Start a goal" onClose={onClose}>
      <label>What are you saving for?<input autoFocus value={name} onChange={e => setName(e.target.value)} /></label>
      <label>How much? ($)<input type="number" inputMode="decimal" step="0.01" value={target} onChange={e => setTarget(e.target.value)} /></label>
      {err && <p className="err" role="alert">{err}</p>}
      <button className="btn wide" disabled={!name.trim() || !(Number(target) > 0)} onClick={go}>Start</button>
    </Sheet>
  );
}

function KidHome({ me, info, refresh, say, go, tick }) {
  const p = info.profile, r = info.rules, gs = p.goals || [], [sel, setSel] = useState(null), g = gs.find(x => x.id === sel) || gs[0] || null, [sheet, setSheet] = useState(null), [ask, setAsk] = useState(false), [coin, setCoin] = useState(false);
  const pct = g ? Math.min(100, Math.round((g.saved / g.target) * 100)) : 0;
  const cancel = async () => { const x = await call(`/goals/${g.id}/cancel`, {}); setAsk(false); refresh(); say(x.detail ? msg(x.detail) : "Goal cancelled. Your savings are back in your wallet."); };
  return (
    <>
      {p.frozen && <p className="card err" role="alert">Spending is paused.</p>}
      <Proposals me={me} refresh={refresh} say={say} limit={2} tick={tick} />
      <section className="stack bal">
        <span className="mute">Wallet</span>
        <p className="big">{money(p.balance)}</p>
        <p className="mute">{money(r.per_purchase_cap)} per buy, {money(r.weekly_budget)} a week</p>
        <button className="btn wide" onClick={() => go("Penny")}>Spend with Penny</button>
      </section>
      <section className="stack savings center">
        {gs.length > 1 && <div role="group" aria-label="Your goals">{gs.map(x => <button key={x.id} className={"pick" + (x.id === g.id ? " on" : "")} aria-pressed={x.id === g.id} onClick={() => { setSel(x.id); setAsk(false); }}>{x.name}</button>)}</div>}
        <div className="pigwrap lg">{coin && <i className="coin" />}<Piggy pct={pct} mood={g && g.saved >= g.target ? "happy" : "idle"} />{g && <span className="dream" aria-hidden="true" style={{ filter: `grayscale(${1 - pct / 100})`, opacity: 0.35 + 0.65 * (pct / 100) }}>{g.emoji || g.name[0]}</span>}</div>
        {g ? <div><p className="big sm">{money(g.saved)}</p><p className="mute">of {money(g.target)} for {g.name}, {pct}% there</p></div> : <p className="mute">Pick something to save for and I'll keep count.</p>}
        {g && <p className={g.bonus_pct > 0 ? "chip coinchip" : "mute"}>{g.bonus_pct > 0 ? `Bonus when you get there: ${money(g.bonus)} (${g.bonus_pct}%)` : "No bonus on this goal yet"}</p>}
        {g && g.watch && <p className="mute">Watching {g.watch_name}: now {money(g.watch_price)}. I'll tell you if it drops.</p>}
        {g && g.saved >= g.target && <p role="status"><b>Goal reached!</b> Waiting for your grown-up to add the bonus.</p>}
        {!g && <button className="btn wide" onClick={() => setSheet("goal")}>Start a goal</button>}
        {g && !ask && <><div className="row"><button className="btn" onClick={() => setSheet("in")}>Add to savings</button><button className="alt" onClick={() => setSheet("out")}>Take out</button></div><div className="row"><button className="link" onClick={() => setSheet("goal")}>Add another goal</button><button className="link" onClick={() => setAsk(true)}>Cancel goal</button></div></>}
        {g && ask && <div className="stack"><p>Cancel and move {money(g.saved)} back to your wallet? You won't get the bonus.</p><div className="row"><button className="alt" onClick={cancel}>Yes, cancel</button><button className="btn" onClick={() => setAsk(false)}>Keep saving</button></div></div>}
      </section>
      {(sheet === "in" || sheet === "out") && g && <MoveSheet me={me} p={p} g={g} start={sheet} onClose={() => setSheet(null)} refresh={refresh} say={say} onCoin={() => { setCoin(true); setTimeout(() => setCoin(false), 1300); }} />}
      <Orders me={me} tick={p.balance} refresh={refresh} />
      {sheet === "goal" && <GoalSheet me={me} onClose={() => setSheet(null)} onMade={setSel} refresh={refresh} say={say} />}
    </>
  );
}

function AdultHome({ me, info, say, refresh, tick, intent, done }) {
  const p = info.profile, [kids, setKids] = useState(null), [add, setAdd] = useState(false), [funds, setFunds] = useState(false), [asks, setAsks] = useState([]);
  const loadAsks = () => call("/inbox").then(r => setAsks((r.approvals || []).filter(x => x.status === "pending")));
  useEffect(() => { loadAsks(); }, [me, tick]);
  const decide = (id, yes) => call(`/approvals/${id}/${yes ? "approve" : "decline"}`, {}).then(r => { say(r.detail ? msg(r.detail) : yes ? "Approved" : "Declined"); loadAsks(); refresh(); });
  useEffect(() => { if (intent?.do === "funds") { setFunds(true); done(); } }, [intent]);  // a notification asked for money
  const load = () => call(`/children/${me}`).then(k => setKids(Array.isArray(k) ? k : []));
  useEffect(() => { load(); }, [me, tick]);
  return (
    <>
      {asks.map(x => <div className="card stack suggest" key={x.id}><div className="spread"><b>{x.who} wants {x.item}</b><span className="chip">{money(x.amount)}</span></div><div className="row"><button className="btn" onClick={() => decide(x.id, true)}>Approve</button><button className="alt" onClick={() => decide(x.id, false)}>Decline</button></div></div>)}
      <Proposals me={me} refresh={refresh} say={say} limit={2} tick={tick} />
      <section className="stack bal"><p className="mute">Your balance</p><p className="big">{money(p.balance)}</p><button className="btn wide" onClick={() => setFunds(true)}>Add money</button></section>
      <div className="spread"><h2 style={{ margin: 0 }}>Kids</h2><button className="plus" aria-label="Add a child" onClick={() => setAdd(true)}><Svg n="plus" /></button></div>
      {kids && kids.length === 0 && <p className="mute">Tap + to add a child.</p>}
      {(kids || []).map(k => <KidCard key={k.id} k={k} me={me} say={say} reload={() => { load(); refresh(); }} />)}
      {funds && <FundsSheet onClose={() => setFunds(false)} />}
      {add && <AddChild me={me} onClose={() => setAdd(false)} onDone={n => { setAdd(false); say(`${n} added`); load(); }} />}
    </>
  );
}

function AddChild({ me, onClose, onDone }) {
  const [f, setF] = useState({ name: "", secret: "", cap: "25", weekly: "40", thr: "15", stores: "Pixel Games, Toy Barn" }), [err, setErr] = useState("");
  const set = (k, v) => setF({ ...f, [k]: v });
  useEffect(() => { const k = e => e.key === "Escape" && onClose(); window.addEventListener("keydown", k); return () => window.removeEventListener("keydown", k); }, []);
  const save = async () => {
    const r = await call("/register", { name: f.name.trim(), secret: f.secret, guardian_id: me, per_purchase_cap: Number(f.cap), weekly_budget: Number(f.weekly), approval_threshold: Number(f.thr), allowed_stores: f.stores.split(",").map(s => s.trim()).filter(Boolean) });
    r.detail ? setErr(msg(r.detail)) : onDone(f.name.trim());
  };
  const num = (k, l) => <label>{l}<input type="number" inputMode="decimal" step="0.01" value={f[k]} onChange={e => set(k, e.target.value)} /></label>;
  return (
    <div className="modal" role="dialog" aria-modal="true" aria-label="Add a child" onClick={onClose}>
      <div className="card stack" onClick={e => e.stopPropagation()}>
        <h2>New profile</h2>
        <label>Name (their username)<input autoFocus value={f.name} onChange={e => set("name", e.target.value)} /></label>
        <label>Password or PIN<input type="password" value={f.secret} onChange={e => set("secret", e.target.value)} /></label>
        <div className="row">{num("cap", "Per buy ($)")}{num("weekly", "Per week ($)")}</div>
        {num("thr", "Ask me above ($)")}
        <label>Stores<input value={f.stores} onChange={e => set("stores", e.target.value)} /></label>
        {err && <p className="err" role="alert">{err}</p>}
        <button className="btn wide" disabled={!f.name.trim() || f.secret.length < 4} onClick={save}>Create profile</button>
        <button className="link wide" onClick={onClose}>Cancel</button>
      </div>
    </div>
  );
}


function Chat({ me, info, say, refresh }) {
  const kid = info.profile.role === "child";
  const ideas = kid ? ["I want to save for something", "Something under $15", "Find me a soccer ball"] : ["Running shoes for Maya", "A birthday gift under $30", "A backpack for school", "Did my last top-up go through?"];
  const [chat, setChat] = useState([]), [text, setText] = useState(""), [busy, setBusy] = useState(false), [img, setImg] = useState(null), [menu, setMenu] = useState(false);
  const end = useRef(), cam = useRef(), up = useRef();
  useEffect(() => { end.current?.scrollIntoView({ block: "nearest" }); }, [chat, busy]);
  useEffect(() => { call("/chat/history").then(r => Array.isArray(r) && setChat(c => (c.length ? c : r))); }, []);  // Penny remembers the last day, even after a reload
  const fresh = async () => { await call("/chat/clear", {}); setChat([]); };
  const attach = async e => {
    const f = e.target.files[0]; e.target.value = ""; setMenu(false); if (!f) return;
    try { setImg({ b64: await shrink(f), url: URL.createObjectURL(f) }); } catch { say("Couldn't read that image."); }
  };
  const open = async o => { const r = await call("/open-link", { profile_id: me, url: o.url }); r.status === "open" ? window.open(r.url, "_blank", "noopener") : say(r.reason || msg(r.detail), r.reason ? "Alerts" : undefined); };
  const send = async m => {
    const t = (m || "").trim(); if ((!t && !img) || busy) return;
    const image_b64 = img?.b64;
    setChat(c => [...c, { who: "me", text: t || "Find this for me.", img: img?.url }]); setText(""); setImg(null); setMenu(false); setBusy(true);
    const r = await call("/chat", { profile_id: me, message: t, image_b64 });
    setChat(c => [...c, { who: "penny", text: r.reply || msg(r.detail), picks: r.picks, order: r.order, err: !r.reply }]); setBusy(false); refresh();
  };
  return (
    <>
      {chat.length === 0 ? (
        <section className="welcome"><Piggy deco pct={60} mood="happy" /><h2>{kid ? "Hi, I'm Penny" : "How can I help?"}</h2><p className="mute">{kid ? "I find things you'll love, help you save, and keep your money on track." : "Tell me what you need. I'll ask if I need details."}</p>
          <div className="ideas">{ideas.map(i => <button key={i} onClick={() => send(i)}>{i}</button>)}</div></section>
      ) : (
        <div className="log chatlog" aria-live="polite">
          <button className="link" onClick={fresh}>New chat</button>
          {chat.map((m, i) => <div key={i} className={`bubble ${m.who}`}>{m.who === "penny" && <Piggy deco tiny pct={50} mood={moodOf(m)} />}{m.img && <img className="thumb" src={m.img} alt="Attached" />}<p>{m.text}</p>{m.order && <OrderCard o={m.order} onDone={refresh} />}{m.picks?.length > 0 && <div className="picks">{m.picks.slice(0, 3).map(o => <Pick key={o.url} o={o} open={open} />)}</div>}</div>)}
          {busy && <div className="bubble penny" role="status" aria-label="Penny is thinking"><Piggy deco tiny pct={50} mood="thinking" /><p className="dots"><i /><i /><i /></p></div>}<span ref={end} className="endmark" />
        </div>
      )}
      <div className="composer">
        {img && <div className="attach"><img src={img.url} alt="Attached" /><button className="icon" aria-label="Remove photo" onClick={() => setImg(null)}><Svg n="close" /></button></div>}
        {menu && <div className="menu" role="menu"><button role="menuitem" onClick={() => cam.current.click()}>Take a photo</button><button role="menuitem" onClick={() => up.current.click()}>Upload a photo</button></div>}
        <div className="inputbar">
          <button className="icon" aria-label="Add media" aria-expanded={menu} onClick={() => setMenu(!menu)}><Svg n="media" /></button>
          <input aria-label="Message" value={text} placeholder={kid ? "Ask Penny" : "What do you need?"} onChange={e => setText(e.target.value)} onKeyDown={e => e.key === "Enter" && send(text)} />
          <button className="sendbtn" aria-label="Send" onClick={() => send(text)}><Svg n="send" /></button>
        </div>
        <input ref={cam} hidden type="file" accept="image/*" capture="environment" onChange={attach} /><input ref={up} hidden type="file" accept="image/*" onChange={attach} />
      </div>
    </>
  );
}

function Alerts({ me, info, refresh, say, tick, intent, done, nav }) {
  const kid = info.profile.role === "child", [d, setD] = useState(null), [hot, setHot] = useState(null);
  const load = async () => {
    const r = await call("/inbox"); if (!r.alerts) return;
    setD(r); const top = Math.max(0, ...r.alerts.map(x => x.id)); if (top) call("/alerts/seen", { upto: top });  // plain notes count as read once shown; ones that ask for something wait
  };
  useEffect(() => { load(); }, [me, tick]);
  useEffect(() => {  // a notification pointed at one item: scroll to it and outline it
    if (!d || !intent?.item) return;
    setHot(intent.item); done();
    setTimeout(() => document.getElementById("item-" + intent.item)?.scrollIntoView({ block: "center", behavior: "smooth" }), 60);
  }, [d, intent]);
  const decide = (id, yes) => call(`/approvals/${id}/${yes ? "approve" : "decline"}`, {}).then(r => { say(r.detail ? msg(r.detail) : yes ? "Approved" : "Declined"); load(); refresh(); });
  const reply = async (p, yes) => { await answer(p, yes, say); load(); refresh(); };
  if (!d) return <><h2>Alerts</h2><div className="stack" aria-busy="true"><Skel h={96} /><Skel h={96} /></div></>;
  const card = (id, extra = "") => ({ id: "item-" + id, className: `card stack${hot === id ? " hot" : ""}${extra}` });
  const asks = x => x.needs_action && !x.handled_at;
  const openA = d.approvals.filter(x => x.status === "pending"), doneA = d.approvals.filter(x => x.status !== "pending");
  const openP = d.proposals.filter(x => x.status === "open"), doneP = d.proposals.filter(x => x.status !== "open");
  const wanted = d.alerts.filter(asks), notes = d.alerts.filter(x => !x.needs_action), doneN = d.alerts.filter(x => x.needs_action && x.handled_at);
  const alertCard = x => (
    <div {...card("a" + x.id)} key={"a" + x.id}><b>{x.title}</b><p style={{ margin: 0 }}>{x.body}</p>
      {!kid && x.goal_id && x.goal_status === "active" && <BonusSlider goalId={x.goal_id} pct={Number(x.goal_pct)} target={Number(x.goal_target)} say={say} onSaved={load} />}
      {!kid && x.goal_id && x.goal_status === "paid" && <p className="mute">Bonus paid: {money(x.goal_bonus)}</p>}
      {asks(x) && !kid && <button className="btn wide" onClick={() => nav({ go: "Home", do: "funds" })}>Add money</button>}
      {x.next_steps && <details><summary className="link">What now?</summary><p className="mute">{x.next_steps}</p></details>}</div>);
  const finished = [
    ...doneA.map(x => ({ id: "u" + x.id, at: x.decided_at, t: `${x.who}: ${x.item}`, l: x.status === "declined" ? "Declined" : "Approved" })),
    ...doneP.map(x => ({ id: "p" + x.id, at: x.decided_at, t: x.title, l: x.status === "accepted" ? "Done" : "Skipped" })),
    ...doneN.map(x => ({ id: "a" + x.id, at: x.handled_at, t: x.title, l: "Done" }))].sort((p, q) => new Date(q.at) - new Date(p.at));
  const empty = !openA.length && !openP.length && !d.alerts.length && !finished.length && !d.activity.length;
  return (
    <>
      <h2>Alerts</h2>
      {empty && <Empty text="All quiet. I'll tell you when something needs you." />}
      {openA.map(x => <div {...card("u" + x.id, " suggest")} key={"u" + x.id}><div className="spread"><b>{x.who} wants {x.item}</b><span className="chip">{money(x.amount)}</span></div><div className="row"><button className="btn" onClick={() => decide(x.id, true)}>Approve</button><button className="alt" onClick={() => decide(x.id, false)}>Decline</button></div></div>)}
      {openP.map(x => <div {...card("p" + x.id, " suggest")} key={"p" + x.id}><b>{x.title}</b><p style={{ margin: 0 }}>{x.body}</p><Trace steps={x.trace} label="How I decided" />
        <div className="row"><button className="btn" onClick={() => reply(x, true)}>{YES[x.kind] || "Yes"}</button><button className="alt" onClick={() => reply(x, false)}>No thanks</button></div></div>)}
      {wanted.map(alertCard)}
      {notes.map(alertCard)}
      {!kid && d.activity.length > 0 && <section className="stack"><h3 className="sub">Your kids, last 24 hours</h3>{d.activity.map(x => <div className="card" key={x.id}><b>{x.who}: {(d.labels[x.event] || x.event).toLowerCase()}</b><p style={{ margin: "4px 0 0" }}>{x.detail}</p></div>)}</section>}
      {finished.length > 0 && <section className="stack"><h3 className="sub">Done</h3>{finished.map(x => <div {...card(x.id, " donecard")} key={x.id}><div className="spread"><b>{x.t}</b><span className="chip">{x.l}</span></div></div>)}<p className="mute">Finished items clear after a day.</p></section>}
    </>
  );
}

function NotifyPrompt({ say, kid }) {  // one gentle nudge on Home until the person chooses
  const [st, setSt] = useState(null), [hide, setHide] = useState(!!localStorage.getItem("np"));
  useEffect(() => { pushState().then(setSt); }, []);
  if (hide || st !== "off") return null;
  const on = async () => { const r = await pushOn(); say(r === "on" ? "Notifications are on" : r === "blocked" ? "Notifications are blocked in your browser settings" : "Couldn't turn on notifications"); setSt(await pushState()); };
  return <section className="card stack prompt"><b>{kid ? "Want a ping when something happens?" : "Get a notification when something needs you"}</b><p className="mute">{kid ? "Allowance, goals and orders." : "Approvals, suggestions and when your balance needs a top-up."}</p>
    <div className="row"><button className="btn" onClick={on}>Turn on</button><button className="alt" onClick={() => { localStorage.setItem("np", "1"); setHide(true); }}>Not now</button></div></section>;
}

function Notifications({ say }) {
  const [st, setSt] = useState("checking"), [busy, setBusy] = useState(false);
  const check = () => pushState().then(setSt);
  useEffect(() => { check(); }, []);
  const on = async () => { setBusy(true); const r = await pushOn(); setBusy(false); say(r === "on" ? "Notifications are on" : r === "blocked" ? "Notifications are blocked in your browser settings" : r === "server" ? "Notifications aren't set up on the server yet" : "Couldn't turn on notifications"); check(); };
  const off = async () => { setBusy(true); await pushOff(); setBusy(false); say("Notifications are off on this device"); check(); };
  const test = async () => { const r = await call("/push/test", {}); say(r.detail ? msg(r.detail) : "Sent. It should arrive in a moment"); };
  const text = { checking: "Checking…", on: "On for this device. You'll get a notification for everything that shows up under Alerts.", off: "Get a notification on this device for everything that shows up under Alerts.",
    blocked: "Blocked. Allow notifications for this site in your browser settings, then come back.", server: "Notifications aren't set up on the server yet.", unsupported: "This browser can't show notifications.",
    ios: "On iPhone and iPad, tap Share, then Add to Home Screen, then open Stash from your Home Screen and turn this on." }[st];
  return (
    <section className="card stack"><div className="spread"><b>Notifications</b>{st === "on" && <span className="chip">On</span>}</div><p className="mute">{text}</p>
      {st === "off" && <button className="btn wide" disabled={busy} onClick={on}>Turn on notifications</button>}
      {st === "on" && <div className="row"><button className="alt" disabled={busy} onClick={test}>Send a test</button><button className="alt" disabled={busy} onClick={off}>Turn off</button></div>}</section>
  );
}

function KidCard({ k, me, say, reload }) {
  const [open, setOpen] = useState(false), [sec, setSec] = useState("Limits"), [d, setD] = useState(null), [f, setF] = useState({ amt: "", week: "", what: "", target: "" }), [pct, setPct] = useState(0);
  useEffect(() => { if (open) call(`/profiles/${k.id}`).then(r => r.rules && setD(r)); }, [open]);
  const gs = k.goals || [], [gsel, setGsel] = useState(null), cur = gsel === "new" ? null : gs.find(x => x.id === gsel) || gs[0] || null;  // cur: the goal on screen; null means a blank "new goal" form
  useEffect(() => { setF(x => cur ? { ...x, what: cur.name, target: String(cur.target) } : { ...x, what: "", target: "" }); setPct(cur ? cur.bonus_pct : 0); }, [cur?.id, cur?.name, cur?.target, cur?.bonus_pct, gsel === "new"]);  // fill the form from the goal; edits stay until the goal changes
  const norm = t => String(t || "").split(/\s+/).filter(Boolean).join(" ").toLowerCase(), match = gs.find(x => norm(x.name) === norm(f.what)) || null;  // a name they already have: only its bonus changes (the server decides the same way)
  const setR = (key, v) => setD({ ...d, rules: { ...d.rules, [key]: v } }), sf = (key, v) => setF({ ...f, [key]: v });
  const done = (r, ok) => { say(r.detail ? msg(r.detail) : ok); reload(); };
  const freeze = async () => { await call(`/freeze/${k.id}`, {}); say(k.frozen ? `${k.name} unfrozen` : `${k.name} frozen`); reload(); };
  const fld = (key, label, type = "number") => <label>{label}<input type={type} inputMode={type === "number" ? "decimal" : undefined} step={type === "number" ? "0.01" : undefined} value={f[key]} onChange={e => sf(key, e.target.value)} /></label>;
  return (
    <section className="card">
      <div className="kidcard">
        <Piggy small pct={k.goal ? Math.min(100, Math.round((k.goal.saved / k.goal.target) * 100)) : 0} />
        <div className="grow"><b>{k.name}</b><p className="mute">{money(k.balance)} wallet{gs.length > 1 ? `, ${money(gs.reduce((t, x) => t + Number(x.saved), 0))} saved across ${gs.length} goals` : k.goal ? `, ${money(k.goal.saved)} of ${money(k.goal.target)} saved` : ""}{k.frozen ? ", frozen" : ""}</p></div>
        <button className="icon" aria-pressed={k.frozen} aria-label={`${k.frozen ? "Unfreeze" : "Freeze"} ${k.name}`} onClick={freeze}><Svg n="snow" s={26} /></button>
        <button className="icon" aria-expanded={open} aria-label={`${open ? "Hide" : "Edit"} limits for ${k.name}`} onClick={() => setOpen(!open)}><Svg n="chev" /></button>
      </div>
      {open && d && (
        <div className="stack" style={{ marginTop: 12 }}>
          <div className="seg" role="group" aria-label="Section">{["Limits", "Money", "Goal"].map(s => <button key={s} className={s === sec ? "on" : ""} onClick={() => setSec(s)}>{s}</button>)}</div>
          {sec === "Limits" && <>
            {[["per_purchase_cap", "Per purchase ($)"], ["weekly_budget", "Per week ($)"], ["approval_threshold", "Ask me above ($)"]].map(([key, l]) => <label key={key}>{l}<input type="number" inputMode="decimal" step="0.01" value={d.rules[key]} onChange={e => setR(key, Number(e.target.value))} /></label>)}
            <label>Stores<input value={d.rules.allowed_stores.join(",")} onChange={e => setR("allowed_stores", e.target.value.split(","))} /></label>
            <button className="btn wide" onClick={() => call(`/rules/${k.id}`, { ...d.rules, allowed_stores: d.rules.allowed_stores.map(s => s.trim()).filter(Boolean) }).then(r => done(r, "Saved"))}>Save</button></>}
          {sec === "Money" && <>
            {fld("amt", "Send from your balance ($)")}<button className="btn wide" disabled={!(Number(f.amt) > 0)} onClick={() => call("/topup", { guardian_id: me, child_id: k.id, amount: Number(f.amt) }).then(r => { done(r, "Sent"); sf("amt", ""); })}>Send</button>
            <hr className="rule" />
            <div className="spread"><b>Weekly allowance</b>{k.allowance > 0 && <span className="mute">{money(k.allowance)} every week</span>}</div>
            {k.allowance > 0 && <p className="mute">Next one: {nextDate(k.allowance_next)}. Sent automatically from your balance.</p>}
            {fld("week", "Amount per week ($)")}
            <div className="row"><button className="btn" disabled={!(Number(f.week) > 0)} onClick={() => call(`/allowance/${k.id}`, { amount: Number(f.week) }).then(r => { done(r, "Weekly allowance set"); sf("week", ""); })}>{k.allowance > 0 ? "Change" : "Set weekly"}</button>
              {k.allowance > 0 && <button className="alt" onClick={() => call(`/allowance/${k.id}`, { amount: 0 }).then(r => done(r, "Weekly allowance stopped"))}>Stop</button>}</div></>}
          {sec === "Goal" && <>
            {gs.length > 0 && <div role="group" aria-label="Goals">{gs.map(x => <button key={x.id} className={"pick" + (cur?.id === x.id ? " on" : "")} aria-pressed={cur?.id === x.id} onClick={() => setGsel(x.id)}>{x.name}</button>)}<button className={"pick" + (!cur ? " on" : "")} aria-pressed={!cur} onClick={() => setGsel("new")}>New goal</button></div>}
            {cur && <><div className="spread"><b>{cur.name}</b><span className="mute">{money(cur.saved)} of {money(cur.target)}</span></div><Bar a={cur.saved} b={cur.target} /></>}
            {fld("what", "What are they saving for?", "text")}
            <label>Target ($)<input type="number" inputMode="decimal" step="0.01" value={match ? String(match.target) : f.target} disabled={!!match} onChange={e => sf("target", e.target.value)} /></label>
            <label>Bonus when they reach it: <b>{pct}%</b> = {money((match ? Number(match.target) : Number(f.target) || 0) * pct / 100)}<input type="range" min="0" max="100" step="5" value={pct} onChange={e => setPct(Number(e.target.value))} aria-valuetext={`${pct} percent`} /></label>
            {match && <p className="mute" role="status">{k.name} already has {match.name}, so only its bonus changes. It won't add another goal.</p>}
            {!match && f.what.trim() && <p className="mute" role="status">This starts a new goal for {k.name}.{gs.length > 0 ? " Their other goals stay as they are." : ""}</p>}
            <p className="mute">Optional. Whatever you can add helps them stay motivated.</p>
            <button className="btn wide" disabled={!f.what.trim() || (!match && !(Number(f.target) > 0)) || (!!match && pct === match.bonus_pct)}
              onClick={() => call("/goals", { child_id: k.id, name: f.what.trim(), target: match ? Number(match.target) : Number(f.target), percent: pct }).then(r => { done(r, r.updated ? (pct ? `Bonus set to ${pct}%` : "Bonus removed") : "Goal started"); if (!r.detail) setGsel(r.id); })}>{match ? "Save bonus" : "Start goal"}</button>
            {cur?.watch && <WatchEditor goal={cur} say={say} onSaved={reload} />}</>}
        </div>
      )}
    </section>
  );
}

function Settings({ info, logout, say }) {
  const p = info.profile;
  return (
    <>
      <h2>Settings</h2>
      <section className="card stack"><div className="spread"><span className="mute">Name</span><b>{p.name}</b></div><div className="spread"><span className="mute">Account</span><b>{p.role === "child" ? "Kid" : "Grown-up"}</b></div></section>
      <Notifications say={say} />
      <button className="alt wide" onClick={logout}>Log out</button>
    </>
  );
}
