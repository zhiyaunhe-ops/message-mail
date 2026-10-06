/* 收件人 / 抄送 chip 输入框的回归测试。
 *
 * 为什么要有这个：createChipField 以前没人测过 —— 它的状态全在闭包里的 values，
 * 一旦「删掉原发件人再填一个新人」的路径上把 values 弄丢，表现是发送时后端报
 * 「收件人为空」（app/mailout.py），前端却看不出来（框里明明有东西）。
 * 这里用一个极小的 DOM 桩把真实的 createChipField 跑起来，断言 serialize()。
 */
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

const partHelpers = slice("const CHIP_SEP_RE", "/** 造一个 chip 输入框");
const partField = slice("function createChipField(ids, label) {", "/* ------------------------------ 写信纯逻辑（可单测）");

const harness = `
/* ---- 极简 DOM 桩：只要 createChipField 用到的那几个方法 ---- */
function fire(node, type, ev) {
  ev = Object.assign({ type, preventDefault() {}, stopPropagation() {}, target: node }, ev || {});
  (node._h[type] || []).forEach((fn) => fn(ev));
  const prop = node["on" + type];
  if (typeof prop === "function") prop(ev);
  return ev;
}
class NodeStub {
  constructor(tag) {
    this.tag = tag; this.children = []; this._h = {}; this._text = ""; this._html = "";
    this.hidden = false; this.value = ""; this.className = ""; this.title = ""; this.style = {};
    const self = this;
    this.classList = {
      contains: (c) => self.className.split(/\\s+/).includes(c),
      add: (c) => { if (!self.classList.contains(c)) self.className = (self.className + " " + c).trim(); },
      remove: (c) => { self.className = self.className.split(/\\s+/).filter((x) => x && x !== c).join(" "); },
      toggle: (c, on) => { const v = on === undefined ? !self.classList.contains(c) : !!on; v ? self.classList.add(c) : self.classList.remove(c); },
    };
  }
  set textContent(v) { this._text = v == null ? "" : String(v); this.children = []; }
  get textContent() { return this._text; }
  set innerHTML(v) { this._html = v == null ? "" : String(v); this.children = []; }
  get innerHTML() { return this._html; }
  appendChild(c) { this.children.push(c); c.parent = this; return c; }
  addEventListener(t, fn) { (this._h[t] = this._h[t] || []).push(fn); }
  focus() { this.focused = true; }
  querySelectorAll(sel) {
    const cls = sel.replace(/^\\./, "");
    return this.children.filter((c) => c.className && c.className.split(/\\s+/).includes(cls));
  }
}
const _nodes = {};
["#cToField", "#cToChips", "#cToMenu", "#cTo"].forEach((s) => { _nodes[s] = new NodeStub("div"); });
const document = { createElement: (t) => new NodeStub(t) };
function $(sel) { return _nodes[sel] || null; }
function el(tag, cls, text) { const n = new NodeStub(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; }
function toast() {}
function markComposeDirty() {}
async function api() { return { items: [] }; }
${partHelpers}
${partField}
globalThis.T = { createChipField, parseAddr, normAddrs, formatAddr, fire, nodes: _nodes };
`;

eval(harness);
const T = globalThis.T;

let pass = 0, fail = 0;
function eq(actual, expected, label) {
  const a = JSON.stringify(actual), e = JSON.stringify(expected);
  if (a === e) { pass++; console.log("  ok   " + label); }
  else { fail++; console.log("  FAIL " + label + "\n       got " + a + "\n       want " + e); }
}

const IDS = { field: "#cToField", chips: "#cToChips", menu: "#cToMenu", input: "#cTo" };
const ALICE = { name: "Alice Chen", email: "alice.chen@partner.example.com" };
const BOB = { name: "Bob", email: "bob.li@partner.example.com" };

function fresh() {
  Object.values(T.nodes).forEach((n) => { n.children = []; n._h = {}; n.value = ""; n.className = ""; n.hidden = true; });
  return T.createChipField(IDS, "收件人");
}
const chipsOf = (f) => T.nodes["#cToChips"].children;
const input = () => T.nodes["#cTo"];

console.log("[回复预填 -> 删掉原发件人 -> 手输新人]");
{
  const f = fresh();
  f.setValues([ALICE]);
  eq(f.serialize(), "Alice Chen <alice.chen@partner.example.com>", "预填一个发件人");
  eq(chipsOf(f).length, 1, "渲染出一个 chip");

  // 点掉那个 ×
  const x = chipsOf(f)[0].children[chipsOf(f)[0].children.length - 1];
  T.fire(x, "click");
  eq(chipsOf(f).length, 0, "删掉后 chip 没了");
  eq(f.serialize(), "", "删掉后序列化为空");

  // 手输新人 + 回车
  input().value = "bob.li@partner.example.com";
  T.fire(input(), "keydown", { key: "Enter" });
  eq(input().value, "", "回车后输入框清空");
  eq(chipsOf(f).length, 1, "新人变成一个 chip");
  eq(f.serialize(), "bob.li@partner.example.com", "serialize 里是新收件人 -> 发送不该报「收件人为空」");
}

console.log("[删掉原发件人 -> 从联想候选里选一个]");
{
  const f = fresh();
  f.setValues([ALICE]);
  T.fire(chipsOf(f)[0].children[chipsOf(f)[0].children.length - 1], "click");
  // 直接调 add 等价于 pick()：候选拿到后 add 走的同一条路
  input().value = "Bob";
  T.fire(input(), "keydown", { key: "Enter" });
  eq(f.isEmpty(), true, "只输名字且没有候选时不加（这属于「没填邮箱」的正常拦截）");
  eq(input().value, "Bob", "拦下来之后原文要留着，别悄悄吃掉");
}

console.log("[逗号 / 分号分隔一次填多个]");
{
  const f = fresh();
  f.setValues([ALICE]);
  T.fire(chipsOf(f)[0].children[chipsOf(f)[0].children.length - 1], "click");
  input().value = "bob.li@partner.example.com, carol.wu@vendor.example.com";
  T.fire(input(), "keydown", { key: "Enter" });
  eq(chipsOf(f).length, 2, "两个都进去了");
  eq(f.serialize(), "bob.li@partner.example.com, carol.wu@vendor.example.com", "两个都序列化出来");
}

console.log("[退格删 chip 后再填]");
{
  const f = fresh();
  f.setValues([ALICE]);
  T.fire(input(), "keydown", { key: "Backspace" });
  eq(f.serialize(), "", "退格删掉最后一个 chip");
  input().value = "bob.li@partner.example.com";
  T.fire(input(), "keydown", { key: "Enter" });
  eq(f.serialize(), "bob.li@partner.example.com", "退格删完还能再填");
}

console.log("[flush：输了一半没回车就点发送]");
{
  const f = fresh();
  input().value = "bob.li@partner.example.com";
  eq(f.flush(), true, "flush 吃掉了输入框里的地址");
  eq(f.serialize(), "bob.li@partner.example.com", "flush 后的地址进得了 serialize");
}

console.log("[重复添加 / 大小写去重]");
{
  const f = fresh();
  f.setValues([ALICE]);
  input().value = "ALICE.CHEN@partner.example.com";
  T.fire(input(), "keydown", { key: "Enter" });
  eq(chipsOf(f).length, 1, "同一个地址（大小写不同）不重复加");
  eq(f.serialize(), "Alice Chen <alice.chen@partner.example.com>", "保留下原样那一条");
}

console.log("[setValues 覆盖旧值]");
{
  const f = fresh();
  f.setValues([ALICE, BOB]);
  f.setValues([BOB]);
  eq(f.serialize(), "Bob <bob.li@partner.example.com>", "setValues 是覆盖不是追加");
}

/* 显示名里的逗号：不加引号的话，「Sample, Steven <a@example.com>」在收件方（和我们自己
   的发信端）都会被拆成两个收件人 —— Family 与 Given。SMTP 拿 Family 去投，服务器回
   550，前端只看到一个 400「部分收件人被拒」。所以序列化时必须补上引号。 */
console.log("[显示名带逗号 / 特殊字符 -> 加引号]");
{
  const sample = { name: "Sample, Steven", email: "sample.name@example.com" };
  eq(T.formatAddr(sample), '"Sample, Steven" <sample.name@example.com>', "逗号名字加引号");
  eq(T.formatAddr({ name: "Anthony Sample (UNIT-FIN)", email: "a@b.c" }), '"Anthony Sample (UNIT-FIN)" <a@b.c>',
    "括号也加引号（Outlook 的写法就这样）");
  eq(T.formatAddr({ name: "Janet Sample", email: "j@b.c" }), "Janet Sample <j@b.c>", "普通名字不加引号");
  eq(T.formatAddr({ name: "", email: "j@b.c" }), "j@b.c", "只有地址就写地址");

  const f = fresh();
  f.setValues([sample]);
  eq(f.serialize(), '"Sample, Steven" <sample.name@example.com>', "chip 序列化后一个人还是一个收件人");
  // 再解析回来仍然是一个人 -> 删除按钮的索引不会错位
  eq(T.normAddrs(f.serialize()), [sample], "序列化结果解析回来还是同一个人（名字里的引号被剥掉）");
  input().value = '"TSE, Sample" <other.sample@example.com>';
  T.fire(input(), "keydown", { key: "Enter" });
  eq(f.serialize(), '"Sample, Steven" <sample.name@example.com>, "TSE, Sample" <other.sample@example.com>',
    "带引号粘贴进来也能收下");
}

console.log("[重复添加：原文清掉但不静默丢]");
{
  const f = fresh();
  f.setValues([ALICE]);
  input().value = "alice.chen@partner.example.com";
  T.fire(input(), "keydown", { key: "Enter" });
  eq(chipsOf(f).length, 1, "重复的不再加一个框");
  eq(input().value, "", "重复的那条（人已经在框里了）清掉输入框");
}

console.log("[候选里的联系人没有邮箱 -> 原文留着]");
{
  const f = fresh();
  // 只输名字，候选恰好只有一个（老逻辑会「唯一候选也认」），但候选没有邮箱
  eq(T.parseAddr("张三"), null, "只有名字解析不出地址");
  input().value = "张三";
  T.fire(input(), "keydown", { key: "Enter" });
  eq(f.isEmpty(), true, "加不进去");
  eq(input().value, "张三", "加不进去就别把用户打的字吃掉");
}

console.log(`\n通过 ${pass} / 失败 ${fail}`);
process.exit(fail ? 1 : 0);
