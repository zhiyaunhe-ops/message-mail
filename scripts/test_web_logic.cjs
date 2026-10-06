/* 从 web/app.js 里切出纯逻辑片段，在 Node 里跑一遍做冒烟测试（不是复制品，是原文） */
const fs = require("fs");
const path = require("path");

const src = fs.readFileSync(path.join(__dirname, "..", "web", "app.js"), "utf8");

function slice(startMark, endMark) {
  const i = src.indexOf(startMark);
  if (i < 0) throw new Error("start mark not found: " + startMark);
  const j = src.indexOf(endMark, i);
  if (j < 0) throw new Error("end mark not found: " + endMark);
  return src.slice(i, j);
}

const partA = slice("const CHIP_SEP_RE", "/** 造一个 chip 输入框");
const partB = slice("function replyTargetOf(m) {", "/* ------------------------------ AI 写正文");
const partC = slice("/* ------------------------------ 写信纯逻辑（可单测）", "/* ------------------------------ 写邮件 / 删除");
const partD = slice("/* ------------------------------ 时间范围", "function groupLabel(");
const partQ = slice("function cidMap(m) {", "/** 外链图片默认屏蔽");
const partS = slice("/* ------------------------------ 设置（AI 接口 + 同步）",
                    "/* ------------------------------ 同步 ------------------------------");

const harness = `
const state = { me: "me@example.com" };
function el(tag, cls, text) { return { tag, cls, text, style: {}, disabled: false, title: "" }; }
function toast() {}
let lastCompose = null;
function openCompose(o) { lastCompose = o; }
/* 时间范围用到的迷你 DOM 桩：筛选框 / 日期框 / 同步按钮 */
const _nodes = {
  "#range": { value: "d30", options: [
    { value: "d30", textContent: "近 1 月" },
    { value: "d7", textContent: "最近 7 天" },
    { value: "all", textContent: "全部时间" },
    { value: "custom", textContent: "自定义日期…" },
  ] },
  "#rangeDate": { value: "", hidden: true },
  "#syncBtn": { title: "" },
  /* 设置面板里的引用头时区输入框 */
  "#cfgTzOffset": { value: "" },
};
function $(sel) { return _nodes[sel] || null; }
function paintSyncBtn() { _nodes["#syncBtn"].title = "t"; }
${partA}
${partB}
${partC}
${partD}
${partQ}
${partS}
globalThis.T = { parseAddr, normAddrs, formatAddr, splitAddrList,
                 replyTargetOf, replyAllTargets, composeReply, replyAllBtn,
                 forwardSubject, composeForward, forwardBtn, draftForwardOf,
                 draftReplyOf, draftContextFields,
                 fileKey, appendFiles, removeFileAt, sumBytes, applyAiOption,
                 displayPayload,
                 localDateStr, daysAgoStr, sinceFromRange, rangeLabel, applyRange, textToHtml, nodes: _nodes,
                 state, getLast: () => lastCompose };
`;
eval(harness);
const T = globalThis.T;

let pass = 0, fail = 0;
function eq(actual, expected, label) {
  const a = JSON.stringify(actual), e = JSON.stringify(expected);
  if (a === e) { pass++; console.log("  ok   " + label); }
  else { fail++; console.log("  FAIL " + label + "\n       got " + a + "\n       want " + e); }
}

console.log("[parseAddr]");
eq(T.parseAddr("zhang.san@example.com"), { name: "", email: "zhang.san@example.com" }, "裸邮箱");
eq(T.parseAddr('Zhang San <sanzhang@x.com>'), { name: "Zhang San", email: "sanzhang@x.com" }, "Name <mail>");
eq(T.parseAddr('"Zhang, San" <w@x.com>'), { name: "Zhang, San", email: "w@x.com" }, "带引号逗号");
eq(T.parseAddr("沃尔特 <w@x.com>"), { name: "沃尔特", email: "w@x.com" }, "中文名");
eq(T.parseAddr("<w@x.com>"), { name: "", email: "w@x.com" }, "只有尖括号");
eq(T.parseAddr("Zhang"), null, "只有名字 -> null");
eq(T.parseAddr("   "), null, "空白 -> null");

console.log("[normAddrs]");
eq(T.normAddrs("a@x.com, b@y.com"), [{ name: "", email: "a@x.com" }, { name: "", email: "b@y.com" }], "逗号串");
eq(T.normAddrs("a@x.com；b@y.com"), [{ name: "", email: "a@x.com" }, { name: "", email: "b@y.com" }], "中文分号");
eq(T.normAddrs("A <a@x.com>, a@X.COM"), [{ name: "A", email: "a@x.com" }], "去重（忽略大小写）");
eq(T.normAddrs(["A <a@x.com>", "b@y.com"]), [{ name: "A", email: "a@x.com" }, { name: "", email: "b@y.com" }], "数组");
eq(T.normAddrs([{ name: "王小明", email: "wang.xiaoming@example.com" }]), [{ name: "王小明", email: "wang.xiaoming@example.com" }], "对象");
eq(T.normAddrs(""), [], "空串");
eq(T.normAddrs(null), [], "null");

/* 下面这些人名 / 域名全是 example.com 占位，不要换成真实往来联系人 */
const inbound = {
  mine: false, folder: "INBOX",
  from: { name: "Alice Chen", email: "alice.chen@partner.example.com" },
  to: [{ name: "", email: "me@example.com" }, { name: "Bob", email: "bob.li@partner.example.com" }],
  cc: [{ name: "Carol", email: "carol.wu@vendor.example.com" }, { name: "Bob", email: "bob.li@partner.example.com" }],
  subject: "Re: HK air cargo",
};

console.log("[回复 / 回复全部：别人发来的]");
eq(T.replyTargetOf(inbound), [{ name: "Alice Chen", email: "alice.chen@partner.example.com" }], "回复=只回发件人");
eq(T.replyAllTargets(inbound), {
  to: [{ name: "Alice Chen", email: "alice.chen@partner.example.com" }, { name: "Bob", email: "bob.li@partner.example.com" }],
  cc: [{ name: "Carol", email: "carol.wu@vendor.example.com" }],
}, "回复全部=发件人+收件人；抄送去掉重复的 Bob；自己不被带上");

const outbound = {
  mine: true, folder: "Sent Items",
  from: { name: "Me", email: "me@example.com" },
  to: [{ name: "Dave", email: "dave.zhang@example.com" }],
  cc: [{ name: "Me", email: "me@example.com" }],
  subject: "Weekly report",
};
console.log("[回复 / 回复全部：自己发出的]");
eq(T.replyTargetOf(outbound), [{ name: "Dave", email: "dave.zhang@example.com" }], "回复=回原收件人");
eq(T.replyAllTargets(outbound), { to: [{ name: "Dave", email: "dave.zhang@example.com" }], cc: [] }, "回复全部：抄送里的自己会被剔掉");

console.log("[composeReply 预填]");
T.composeReply(inbound, true);
eq(T.getLast().to.length, 2, "to 预填 2 人");
eq(T.getLast().cc.length, 1, "cc 预填 1 人");
eq(T.getLast().subject, "Re: HK air cargo", "已有 Re: 不重复加");
eq(T.getLast().reply, { folder: "INBOX", uid: undefined }, "带 reply 头信息");

const single = { mine: false, folder: "INBOX", from: { name: "Solo", email: "solo@example.com" },
                 to: [{ email: "me@example.com" }], cc: [], subject: "hi" };
console.log("[回复全部按钮置灰]");
eq(T.replyAllBtn(single).disabled, true, "只有我一个收件人 -> 置灰");
eq(T.replyAllBtn(inbound).disabled, false, "有其他人 -> 可点");

console.log("[草稿里的回复上下文]");
eq(T.draftReplyOf({ headers: { x_reply: { folder: "INBOX", uid: 1737102008 } } }),
   { folder: "INBOX", uid: 1737102008 }, "草稿带 x_reply -> 恢复回复上下文");
eq(T.draftReplyOf({ headers: { x_reply: { folder: "INBOX", uid: 0 } } }), null, "uid 为 0 视为没有");
eq(T.draftReplyOf({ headers: {} }), null, "普通草稿没有回复上下文");
eq(T.draftReplyOf({}), null, "没有 headers 也不报错");
eq(T.draftContextFields({ folder: "Drafts", uid: 12 }, { folder: "INBOX", uid: 7 }),
   { draft_folder: "Drafts", draft_uid: 12, reply_folder: "INBOX", reply_uid: 7,
     forward_folder: "", forward_uid: 0 },
   "存草稿时同时带上「顶哪一版」和「回复谁」");
eq(T.draftContextFields(null, null),
   { draft_folder: "", draft_uid: 0, reply_folder: "", reply_uid: 0, forward_folder: "", forward_uid: 0 },
   "新写的信两个都为空");

/* ---- 转发 ---- */
console.log("[转发]");
eq(T.forwardSubject("HK air cargo"), "Fwd: HK air cargo", "加 Fwd: 前缀");
eq(T.forwardSubject("Fwd: HK air cargo"), "Fwd: HK air cargo", "已有 Fwd: 不叠");
eq(T.forwardSubject("FW: HK air cargo"), "FW: HK air cargo", "Outlook 的 FW: 也算已有");
eq(T.forwardSubject(""), "Fwd: ", "没主题也不炸");

T.composeForward({ folder: "INBOX", uid: 1737102010, subject: "Weekly report", attachments: [] });
eq(T.getLast().to, [], "转发的收件人留空（等人填）");
eq(T.getLast().cc, [], "抄送也留空");
eq(T.getLast().subject, "Fwd: Weekly report", "主题带 Fwd:");
eq(T.getLast().forward, { folder: "INBOX", uid: 1737102010 }, "带 forward 上下文给后端拼引用块");
eq(T.getLast().reply, undefined, "转发不是回复：不挂 reply（后端就不会加 In-Reply-To）");

eq(T.draftForwardOf({ headers: { x_forward: { folder: "INBOX", uid: 42 } } }),
   { folder: "INBOX", uid: 42 }, "草稿带 x_forward -> 恢复转发上下文");
eq(T.draftForwardOf({ headers: { x_reply: { folder: "INBOX", uid: 42 } } }), null, "回复上下文不当成转发");
eq(T.draftForwardOf({}), null, "没有 headers 也不报错");
eq(T.draftContextFields({ folder: "Drafts", uid: 3 }, null, { folder: "INBOX", uid: 9 }),
   { draft_folder: "Drafts", draft_uid: 3, reply_folder: "", reply_uid: 0,
     forward_folder: "INBOX", forward_uid: 9 },
   "转发草稿带的是 forward_* 而不是 reply_*");

eq(T.forwardBtn({ folder: "INBOX", uid: 7 }).disabled, false, "有 folder/uid 就能转发");
eq(T.forwardBtn({}).disabled, true, "连 folder/uid 都没有 -> 置灰（点不了）");

/* ---- 附件列表 ---- */
const f = (name, size, mtime) => ({ name, size, lastModified: mtime || 1 });
console.log("[附件：fileKey / appendFiles / removeFileAt / sumBytes]");
eq(T.fileKey(f("a.pdf", 10, 5)), "a.pdf|10|5", "同名同大小同时间 -> 同一个 key");
eq(T.fileKey(f("a.pdf", 11, 5)) !== T.fileKey(f("a.pdf", 10, 5)), true, "大小不同 -> 不同 key");

let list = [];
eq(T.appendFiles(list, [f("a.pdf", 10), f("b.docx", 20)]), 2, "首次追加 2 个");
eq(list.map((x) => x.name), ["a.pdf", "b.docx"], "顺序保持添加顺序");
eq(T.appendFiles(list, [f("b.docx", 20), f("c.png", 30)]), 1, "重复的不再加");
eq(list.length, 3, "现在 3 个附件");
eq(T.appendFiles(list, null), 0, "空输入不报错");
eq(T.sumBytes(list), 60, "大小合计 10+20+30");
eq(T.removeFileAt(list, 1), true, "能删掉中间那个");
eq(list.map((x) => x.name), ["a.pdf", "c.png"], "删完顺序不乱");
eq(T.removeFileAt(list, 9), false, "越界删除返回 false");
eq(T.removeFileAt(list, -1), false, "负下标也返回 false");

/* ---- AI 方案套用 ---- */
const opt = { title: "正式", subject: "ERP 上线时间确认", body: "王总：\n\n……\n\n顺颂商祺" };
console.log("[applyAiOption]");
eq(T.applyAiOption({ subject: "", body: "" }, opt, true),
   { body: opt.body, subject: opt.subject, subjectUsed: true }, "勾了补主题 + 原主题为空 -> 填主题");
eq(T.applyAiOption({ subject: "我写的", body: "" }, opt, true),
   { body: opt.body, subject: opt.subject, subjectUsed: true }, "勾了补主题 -> 覆盖原主题");
eq(T.applyAiOption({ subject: "我写的", body: "" }, opt, false),
   { body: opt.body, subject: "我写的", subjectUsed: false }, "不勾 -> 主题原样不动");
eq(T.applyAiOption({ subject: "", body: "" }, { title: "无主题方案", body: "X" }, true),
   { body: "X", subject: "", subjectUsed: false }, "方案没给主题 -> 不标记已用");
eq(T.applyAiOption(null, { body: "Y" }, true),
   { body: "Y", subject: "", subjectUsed: false }, "fields 为空也不炸");

/* ---- 时间范围（筛选框 ↔ 手动同步共用的那一份 since） ---- */
const expectDaysAgo = (n) => {
  const d = new Date(Date.now() - n * 86400000);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
};
console.log("[sinceFromRange]");
eq(T.sinceFromRange("all"), "all", "全部时间");
eq(T.sinceFromRange("d30"), expectDaysAgo(30), "近 1 月 = 30 天前");
eq(T.sinceFromRange("d7"), expectDaysAgo(7), "近 7 天");
eq(T.sinceFromRange("d1"), expectDaysAgo(1), "近 1 天");
eq(T.sinceFromRange("custom", "2026-08-15"), "2026-08-15", "自定义日期原样用");
eq(T.sinceFromRange("custom"), expectDaysAgo(30), "自定义没填 -> 退回近 1 月");
eq(T.sinceFromRange("2026-07-01"), "2026-07-01", "旧的「某天以来」写法仍兼容");
eq(T.sinceFromRange(""), "all", "空值 -> 不限时间");
// 本地日期，不是 UTC：东八区晚上用 toISOString 会把当天算成昨天
eq(T.sinceFromRange("d0"), T.localDateStr(new Date()), "d0 = 今天（本地日期）");

console.log("[rangeLabel]");
eq(T.rangeLabel("d30"), "近 1 月", "近 1 月");
eq(T.rangeLabel("d7"), "近 7 天", "近 7 天");
eq(T.rangeLabel("all"), "全部时间", "全部时间");
eq(T.rangeLabel("custom", "2026-08-15"), "2026-08-15 以来", "自定义带日期");
eq(T.rangeLabel("custom", "all"), "自定义日期", "自定义还没选");
eq(T.rangeLabel("2026-07-01"), "2026-07-01", "不认识的取值原样返回（别显示 undefined）");

console.log("[applyRange]");
T.applyRange("custom", "2026-08-15");
eq(T.nodes["#rangeDate"].hidden, false, "自定义 -> 日期框露出来");
eq(T.nodes["#rangeDate"].value, "2026-08-15", "日期框填上选的值");
eq(T.state.since, "2026-08-15", "since 跟着日期走");
T.applyRange("all");
eq(T.nodes["#rangeDate"].hidden, true, "切回全部时间 -> 日期框收起");
eq(T.nodes["#range"].value, "all", "下拉框同步到 all");
T.applyRange("d7");
eq(T.nodes["#range"].value, "d7", "下拉框同步到 d7");
eq(T.nodes["#rangeDate"].hidden, true, "非自定义时日期框始终收起");

/* ---- 纯文本引用 -> blockquote（会话视图折叠的前提） ---- */
const replyText = [
  "Hi Anthony,",
  "",
  "4:00 pm works.",
  "",
  "----- 原始邮件 -----",
  "发件人: A <a@partner.example.com>",
  "时间: 2026-09-14 11:29",
  "主题: RE: subject",
  "",
  "> Dear Sender,",
  "> ",
  "> Can we do it at 4:00pm?",
  "> Best regards,",
  "> Anthony Sample, Manager, Financial Accounting, T +852 0000 0000, Example Website",
].join("\n");

console.log("[textToHtml 引用块]");
const qHtml = T.textToHtml(replyText, {});
eq((qHtml.match(/<blockquote class="mail-quote">/g) || []).length, 1, "整个引用包成一个 blockquote");
eq(qHtml.includes('<blockquote class="mail-quote">----- 原始邮件 -----'), true, "blockquote 紧贴分隔头打开");
eq(qHtml.includes("&gt; Dear") || qHtml.includes("&gt;Can"), false, "引用行里的 > 前缀被剥掉");
eq(qHtml.includes("<br>Dear Sender,"), true, "引用内容以正常文本呈现");
eq(qHtml.trim().endsWith("</blockquote>"), true, "引用到结尾");
{
  // foldQuotes 的门槛：blockquote 文本 >= 120 字符才会折叠
  const qText = qHtml.slice(qHtml.indexOf("<blockquote")).replace(/<[^>]+>/g, "");
  eq(qText.length >= 120, true, "引用文本够长，foldQuotes 能折叠");
}
const noQuote = T.textToHtml("plain line\nsee https://x.com", {});
eq(/<blockquote/.test(noQuote), false, "没引用就不包 blockquote");
eq(noQuote, 'plain line<br>see <a href="https://x.com" target="_blank" rel="noopener noreferrer">https://x.com</a>', "普通文本只做转义+链接+换行");
const outlook = T.textToHtml("body\n-----Original Message-----\nFrom: X <x@partner.example.com>\nSent: today\nstuff stuff stuff stuff stuff", {});
eq((outlook.match(/<blockquote class="mail-quote">/g) || []).length, 1, "Outlook 分隔头（无 > 前缀）也包进引用");
eq(outlook.includes("From: X"), true, "分隔头之后整段算引用历史");
const stray = T.textToHtml("text\n> quoted bit\nmore text", {});
eq((stray.match(/<\/blockquote>/g) || []).length, 1, "正文里零散的 > 行单独成块，闭合不泄漏");

/* ------------------------------ 设置：引用头时区 ------------------------------ */
{
  T.nodes["#cfgTzOffset"].value = "  +08:00  ";
  eq(T.displayPayload(), { utc_offset: "+08:00" }, "时区偏移提交前去掉两头空格");
  T.nodes["#cfgTzOffset"].value = "";
  eq(T.displayPayload(), { utc_offset: "" }, "留空就是留空（后端解读为按服务器本地时区）");
  T.nodes["#cfgTzOffset"].value = "-07:00";
  eq(T.displayPayload(), { utc_offset: "-07:00" }, "负偏移原样送出去");
  eq(Object.keys(T.displayPayload()).length, 1, "只送 utc_offset 一个字段，不会顺手动别的段");
  T.nodes["#cfgTzOffset"].value = "Asia/Hong_Kong";
  eq(T.displayPayload(), { utc_offset: "Asia/Hong_Kong" }, "IANA 名前端不拦，交给后端 400 说清楚");
}
console.log(`\n通过 ${pass} / 失败 ${fail}`);
process.exit(fail ? 1 : 0);
