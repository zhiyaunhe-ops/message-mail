/* Mail WebUI — 5 主题 + 三栏阅读器 */
(function () {
  "use strict";

  const THEMES = [
    { id: "ink", name: "宣纸水墨", desc: "宋体 × 朱砂 × 宣纸质地", sw: ["#f2ede1", "#a3302a", "#1f1c17"] },
    { id: "swiss", name: "瑞士极简", desc: "黑白网格 × 国际主义排版", sw: ["#ffffff", "#0b57ff", "#111111"] },
    { id: "neon", name: "暗夜霓虹", desc: "深空底 × 紫青辉光", sw: ["#111528", "#8b5cf6", "#22d3ee"] },
    { id: "terminal", name: "复古终端", desc: "CRT 绿字 × 扫描线", sw: ["#080c08", "#b6ff3a", "#4dff88"] },
    { id: "glass", name: "琉璃柔彩", desc: "毛玻璃 × 粉紫渐变", sw: ["#fdeef6", "#ff5f9e", "#7c6cf5"] },
  ];

  const state = {
    // —— 邮件模块 ——
    folder: "INBOX",
    q: "",
    // 时间范围：range 是筛选框的取值（d30 / d7 / all / custom），since 是它算出来的日期界线。
    // 筛选列表、会话、联系人、手动同步全都读同一份 since —— 点「同步」拉的就是筛选框这个范围。
    range: "d30",
    rangeDate: "",          // range === "custom" 时的自定义日期界线
    since: "",
    unreadOnly: false,
    limit: 60,
    offset: 0,
    total: 0,
    items: [],
    current: null,
    folders: [],
    bodyMode: "rich",       // rich | text | source
    showExternal: false,    // 是否显示外链图片（防追踪，默认屏蔽）
    viewMode: "message",    // message | thread
    category: "all",        // all | personal | boring | meeting | automated | promotion
    me: "",                 // 自己的地址（回复全部时要把自己剔掉）
    // —— 多邮箱 ——
    // accounts      : /api/accounts 的账号摘要列表（不含明文密码）
    // activeAccount : 当前生效的账号名；accountEdit: 邮箱管理弹窗里正在编辑的账号名（"" = 新增）
    accounts: [],
    activeAccount: "",
    accountsFile: "",
    accountEdit: "",
    // —— 写邮件 ——
    // reply   : {folder, uid} 表示这封是回复
    // forward : {folder, uid} 表示这封是转发（与 reply 互斥：转发不挂 In-Reply-To）
    // draft : {folder, uid} 服务器草稿箱里对应的那一版（自动保存时替换它，避免堆积）
    // dirty : 内容被改过，需要落草稿；timer: 防抖句柄
    // saving: 正在路上的那次自动保存（发送前要 await 它，见 submitCompose）
    // files : 已添加的附件（File 对象数组，自己维护才能逐个移除）
    // seq   : 写信框的代数。异步动作（例如转发时去取原附件）回来时比对一下，
    //         免得窗口已经关了、或者又开了另一封，附件落进不该落的窗口。
    // ai    : 最近一次 AI 生成的结果，收起面板后仍留着，可以再展开
    compose: { reply: null, forward: null, draft: null, dirty: false, timer: null, saving: null, sending: false, files: [], seq: 0, ai: null },
  };

  const DRAFT_DEBOUNCE = 1800;  // 停止输入 1.8s 后自动存草稿

  // 收件人 / 抄送 chip 输入框（DOM 就绪后在 bind() 里实例化）
  const chips = { to: null, cc: null };

  const $ = (sel) => document.querySelector(sel);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  };

  /* ------------------------------ 工具 ------------------------------ */
  function fmtBytes(n) {
    if (!n) return "0 B";
    const u = ["B", "KB", "MB", "GB"];
    let i = 0, v = n;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return (i === 0 ? v : v.toFixed(1)) + " " + u[i];
  }

  function parseDate(s) {
    if (!s) return null;
    const d = new Date(s);
    return isNaN(d.getTime()) ? null : d;
  }

  /* ------------------------------ 时间范围 ------------------------------ */
  // 用本地日期而不是 toISOString()：东八区下 UTC 会把今天算成昨天，
  // 「近 1 天」的界线就会漏掉当天收到的邮件（后端 IMAP SINCE 也是本地日期）。
  function localDateStr(d) {
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  }

  function daysAgoStr(n) {
    return localDateStr(new Date(Date.now() - n * 86400000));
  }

  /** 筛选框取值 → 日期界线："all" 表示不限时间。 */
  function sinceFromRange(value, customDate) {
    if (value === "all") return "all";
    if (value === "custom") return customDate || daysAgoStr(30);
    if (/^d\d+$/.test(value || "")) return daysAgoStr(parseInt(value.slice(1), 10));
    return value || "all";     // 兼容「某个固定日期以来」的写法
  }

  /** 给按钮提示 / 同步结果提示用的人类可读范围名。 */
  function rangeLabel(value, since) {
    const known = { all: "全部时间", d7: "近 7 天", d30: "近 1 月" };
    if (known[value]) return known[value];
    if (value === "custom") return since && since !== "all" ? `${since} 以来` : "自定义日期";
    const sel = $("#range");
    const opt = sel && [...sel.options].find((o) => o.value === value);
    return opt ? opt.textContent : String(value || "");
  }

  /** 时间范围变了：同步 DOM、重算 since、刷新按钮提示。 */
  function applyRange(value, customDate) {
    state.range = value;
    const inp = $("#rangeDate");
    if (value === "custom") {
      if (inp) {
        inp.hidden = false;
        if (!inp.value) inp.value = customDate || daysAgoStr(30);
        state.rangeDate = inp.value;
      }
      state.since = sinceFromRange("custom", state.rangeDate || customDate);
    } else {
      if (inp) inp.hidden = true;
      state.rangeDate = "";
      state.since = sinceFromRange(value);
    }
    const sel = $("#range");
    if (sel && sel.value !== value) sel.value = value;
    paintSyncBtn();
  }

  function fmtTime(s) {
    const d = parseDate(s);
    if (!d) return "";
    const now = new Date();
    const sameDay = d.toDateString() === now.toDateString();
    if (sameDay) return String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
    if (d.getFullYear() === now.getFullYear())
      return String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
    return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
  }

  function fmtFull(s) {
    const d = parseDate(s);
    if (!d) return s || "";
    const wd = "日一二三四五六"[d.getDay()];
    return `${d.getFullYear()} 年 ${d.getMonth() + 1} 月 ${d.getDate()} 日（周${wd}） ` +
      `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  }

  function groupLabel(s) {
    const d = parseDate(s);
    if (!d) return "未知日期";
    const now = new Date();
    if (d.toDateString() === now.toDateString()) return "今天";
    const y = new Date(now.getTime() - 86400000);
    if (d.toDateString() === y.toDateString()) return "昨天";
    if (d.getFullYear() === now.getFullYear()) return `${d.getMonth() + 1} 月 ${d.getDate()} 日`;
    return `${d.getFullYear()} 年 ${d.getMonth() + 1} 月`;
  }

  function esc(s) { return String(s == null ? "" : s); }

  const CAT_META = {
    meeting: { label: "会议", emoji: "📅", cls: "cat-meeting" },
    automated: { label: "系统", emoji: "🤖", cls: "cat-automated" },
    promotion: { label: "推广", emoji: "📢", cls: "cat-promotion" },
  };
  function catBadge(m) {
    const meta = CAT_META[m.category];
    if (!meta) return null;
    return el("span", "cat-badge " + meta.cls, `${meta.emoji} ${meta.label}`);
  }
  function initials(name, email) {
    const n = (name || email || "?").trim();
    return (n[0] || "?").toUpperCase();
  }

  let toastTimer = null;
  function toast(msg, ms) {
    const t = $("#toast");
    t.textContent = msg;
    t.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.remove("show"), ms || 2600);
  }

  async function api(path, opts) {
    const res = await fetch(path, opts);
    if (!res.ok) {
      const txt = await res.text().catch(() => "");
      let msg = txt.slice(0, 200);
      try {
        const j = JSON.parse(txt);            // FastAPI 的 {"detail": "..."} 比裸 JSON 好看
        if (j && j.detail) msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
      } catch (e) {}
      const err = new Error(msg || `HTTP ${res.status}`);
      err.status = res.status;               // 调用方要区分 429（后台同步中）这类情况
      throw err;
    }
    return res.json();
  }

  const jsonPost = (url, data, opts) => api(url, Object.assign({
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  }, opts || {}));

  /* ------------------------------ 主题 ------------------------------ */
  function applyTheme(id) {
    const t = THEMES.find((x) => x.id === id) || THEMES[0];
    document.documentElement.setAttribute("data-theme", t.id);
    $("#themeName").textContent = t.name;
    try { localStorage.setItem("mail.theme", t.id); } catch (e) {}
    document.querySelectorAll(".theme-opt").forEach((n) =>
      n.classList.toggle("is-active", n.dataset.theme === t.id)
    );
  }

  function buildThemeMenu() {
    const menu = $("#themeMenu");
    menu.innerHTML = "";
    THEMES.forEach((t) => {
      const b = el("button", "theme-opt");
      b.dataset.theme = t.id;
      const sw = el("span", "theme-sw");
      t.sw.forEach((c) => { const i = el("i"); i.style.background = c; sw.appendChild(i); });
      const box = el("span");
      box.appendChild(el("div", "theme-opt-name", t.name));
      box.appendChild(el("div", "theme-opt-desc", t.desc));
      b.appendChild(sw);
      b.appendChild(box);
      b.appendChild(el("span", "theme-check", "✓"));
      b.addEventListener("click", () => { applyTheme(t.id); menu.classList.remove("open"); });
      menu.appendChild(b);
    });

    // CRT 扫描线开关（默认关：扫描线会盖在图片上，像蒙了一层条纹阴影）
    let crtOn = false;
    try { crtOn = localStorage.getItem("mail.crt") === "on"; } catch (e) {}
    document.documentElement.classList.toggle("crt-on", crtOn);
    const wrap = el("label", "theme-crt");
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = crtOn;
    cb.addEventListener("change", () => {
      document.documentElement.classList.toggle("crt-on", cb.checked);
      try { localStorage.setItem("mail.crt", cb.checked ? "on" : "off"); } catch (e) {}
    });
    wrap.appendChild(cb);
    wrap.appendChild(el("span", null, "CRT 扫描线（复古终端）"));
    menu.appendChild(wrap);
  }

  /* ------------------------------ 目录 ------------------------------ */
  function folderLabel(name) {
    const map = { INBOX: "收件箱", "Sent Items": "已发送", Drafts: "草稿箱", Trash: "已删除", Archive: "归档", "Junk E-mail": "垃圾邮件", "Virus Items": "病毒箱", Scheduled: "定时发送" };
    return map[name] || name;
  }

  const LS_FOLDERS = "mail.folders.cache";

  /** 先用上次的目录快照把侧栏画出来（秒开），网络回来后再覆盖 —— stale-while-revalidate。 */
  function paintCachedFolders() {
    try {
      const arr = JSON.parse(localStorage.getItem(LS_FOLDERS) || "null");
      if (Array.isArray(arr) && arr.length) {
        state.folders = arr;
        renderFolders();
      }
    } catch (e) {}
  }

  async function loadFolders() {
    try {
      const data = await api("/api/folders");
      state.folders = data.folders || [];
      try { localStorage.setItem(LS_FOLDERS, JSON.stringify(state.folders)); } catch (e) {}
    } catch (e) {
      // 失败时保留本地快照，别把侧栏清空
      if (!state.folders.length) state.folders = [{ name: "INBOX", total: 0, unread: 0 }];
    }
    renderFolders();
  }

  function renderFolders() {
    const ul = $("#folders");
    ul.innerHTML = "";
    state.folders.forEach((f) => {
      const li = el("li", "folder-item" + (f.name === state.folder ? " is-active" : ""));
      li.appendChild(el("span", "folder-name", folderLabel(f.name)));
      li.appendChild(el("span", "folder-count", f.unread ? `${f.total} · ${f.unread} 未读` : String(f.total)));
      li.addEventListener("click", () => selectFolder(f.name));
      // 系统目录（收件箱 / 已发送 / 草稿 / 回收站）后端不允许删，这里也不渲染按钮
      if (!protectedFolders().has(f.name)) {
        const del = el("button", "folder-del", "×");
        del.type = "button";
        del.title = `删除目录「${f.name}」`;
        del.addEventListener("click", (e) => {
          e.stopPropagation();          // 别顺带切到该目录
          deleteFolder(f);
        });
        li.appendChild(del);
      }
      ul.appendChild(li);
    });
  }

  /** 系统目录：不给删除按钮。与后端 `/api/folders` 的 _protected_folders() 同源 ——
      INBOX + 当前账号 aliases 里的 sent/drafts/trash。按账号动态算，换服务商
      （目录名可能是 "Sent"、"[Gmail]/Sent Mail" 等）也不会误给一个点了必报 400 的按钮。 */
  function protectedFolders() {
    const a = activeAccountInfo();
    const al = (a && a.aliases) || {};
    return new Set(["INBOX", al.inbox, al.sent, al.drafts, al.trash].filter(Boolean));
  }

  async function deleteFolder(f) {
    if (!confirm(
      `删除目录「${f.name}」？\n\n` +
      `会同时删除服务器上的这个目录（连同里面 ${f.total || 0} 封邮件）和本地索引，不可逆。\n` +
      `服务器端删除失败就只清本地。`
    )) return;
    try {
      const r = await api(`/api/folders/${encodeURIComponent(f.name)}`, { method: "DELETE" });
      toast(`已删除「${f.name}」（本地清掉 ${r.local ? r.local.removed : 0} 行）`, 4000);
      if (state.folder === f.name) selectFolder("INBOX");
      await loadFolders();
    } catch (e) {
      toast("删除目录失败：" + e.message, 5000);
    }
  }

  function selectFolder(name) {
    state.folder = name;
    state.offset = 0;
    state.current = null;
    setReading(false);            // 换目录就回到列表（手机上是整屏覆盖层）
    $("#listTitle").textContent = folderLabel(name) + (state.viewMode === "thread" ? " · 会话" : "");
    renderFolders();
    $("#reader").innerHTML =
      state.viewMode === "thread" ? '<div class="empty">选择一条会话开始阅读</div>' : '<div class="empty">选择一封邮件开始阅读</div>';
    loadCurrent(false);
  }

  function loadCurrent(append) {
    return state.viewMode === "thread" ? loadThreads(append) : loadMessages(append);
  }

  // 空列表提示：目录其实有邮件但被当前筛选（时间范围/搜索/未读/分类）挡掉时，给出原因和一键解除
  function renderEmptyList(kind) {
    const list = $("#messageList");
    const ftotal = (state.folders.find((f) => f.name === state.folder) || {}).total || 0;
    let html = `<div class="empty">没有匹配的${kind}`;
    if (ftotal > 0) {
      const tips = [];
      if (state.since !== "all")
        tips.push('<a href="#" id="emptyAllLink">切换到全部时间</a>');
      if (state.q) tips.push('<a href="#" id="emptyClearQ">清空搜索</a>');
      if (state.unreadOnly) tips.push('<a href="#" id="emptyClearUnread">取消只看未读</a>');
      if (state.category !== "all") tips.push('<a href="#" id="emptyClearCat">取消分类筛选</a>');
      if (tips.length)
        html += `<br><span class="empty-hint">该目录共有 ${ftotal} 封邮件，只是没命中当前筛选 — ${tips.join(" · ")}</span>`;
    }
    html += "</div>";
    list.innerHTML = html;
    const wire = (id, fn) => {
      const a = $(id);
      if (a) a.addEventListener("click", (e) => { e.preventDefault(); fn(); });
    };
    wire("#emptyAllLink", () => { $("#range").value = "all"; $("#range").dispatchEvent(new Event("change", { bubbles: true })); });
    wire("#emptyClearQ", () => { $("#search").value = ""; state.q = ""; loadCurrent(false); });
    wire("#emptyClearUnread", () => { $("#unreadBtn").click(); });
    wire("#emptyClearCat", () => { $("#category").value = "all"; $("#category").dispatchEvent(new Event("change", { bubbles: true })); });
  }

  /* ------------------------------ 列表 ------------------------------ */
  async function loadMessages(append) {
    const list = $("#messageList");
    if (!append) { list.innerHTML = '<div class="empty">加载中…</div>'; state.offset = 0; }

    const params = new URLSearchParams({
      folder: state.folder,
      since: state.since === "all" ? "" : state.since,
      limit: String(state.limit),
      offset: String(state.offset),
      order: "desc",
    });
    if (state.q) params.set("q", state.q);
    if (state.unreadOnly) params.set("unread_only", "true");
    if (state.category === "boring") params.set("boring", "true");
    else if (state.category === "personal") params.set("boring", "false");
    else if (state.category !== "all") params.set("category", state.category);
    if (state.since === "all") params.delete("since");

    let data;
    try {
      data = await api("/api/messages?" + params.toString());
    } catch (e) {
      list.innerHTML = `<div class="empty">加载失败：${e.message}</div>`;
      return;
    }
    state.total = data.total;
    state.items = append ? state.items.concat(data.items) : data.items;

    list.innerHTML = "";
    if (!state.items.length) {
      renderEmptyList("邮件");
      $("#listCount").textContent = "0 封";
      $("#moreBtn").style.display = "none";
      return;
    }
    let lastGroup = "";
    state.items.forEach((m) => {
      const g = groupLabel(m.date);
      if (g !== lastGroup) { list.appendChild(el("div", "date-sep", g)); lastGroup = g; }

      const row = el("div", "msg-row" + (m.unread ? " is-unread" : "") +
        (m.is_boring ? " is-boring" : "") +
        (state.current && state.current.uid === m.uid && state.current.folder === m.folder ? " is-selected" : ""));
      row.dataset.uid = m.uid;
      row.dataset.folder = m.folder;

      const line = el("div", "msg-line");
      line.appendChild(el("span", "msg-from", m.from.name || m.from.email));
      const badge = catBadge(m);
      if (badge) line.appendChild(badge);
      line.appendChild(el("span", "msg-time", fmtTime(m.date)));
      row.appendChild(line);
      row.appendChild(el("div", "msg-subject", m.subject || "(无主题)"));
      row.appendChild(el("div", "msg-snippet", m.snippet || ""));

      const tags = el("div", "msg-tags");
      if (m.unread) tags.appendChild(el("span", "badge-unread"));
      if (m.has_attachment) tags.appendChild(el("span", "tag attach", `📎 ${m.attachment_count}`));
      if (m.folder !== state.folder) tags.appendChild(el("span", "tag", folderLabel(m.folder)));
      if (tags.children.length) row.appendChild(tags);

      row.addEventListener("click", () => openMessage(m.folder, m.uid, row));
      list.appendChild(row);
    });

    $("#listCount").textContent = `${state.total} 封`;
    $("#moreBtn").style.display = state.items.length < state.total ? "" : "none";
    $("#moreBtn").textContent = `加载更多（还有 ${state.total - state.items.length} 封）`;
  }

  /* ------------------------------ 阅读 ------------------------------ */
  // 窄屏（手机）上阅读区是整屏覆盖层：靠 body.is-reading 滑出来，返回按钮收起来。
  function setReading(on) {
    document.body.classList.toggle("is-reading", !!on);
  }

  /** 只有在窄屏才看得见的「‹ 返回列表」，插在阅读区最前面。 */
  function readerBackBar(label) {
    const bar = el("div", "reader-back-bar");
    const b = el("button", "reader-back", `‹ ${label || "返回列表"}`);
    b.type = "button";
    b.onclick = () => setReading(false);
    bar.appendChild(b);
    return bar;
  }

  async function openMessage(folder, uid, row) {
    document.querySelectorAll(".msg-row").forEach((n) => n.classList.remove("is-selected"));
    if (row) row.classList.add("is-selected");

    setReading(true);
    const reader = $("#reader");
    reader.innerHTML = '<div class="empty">加载中…</div>';
    try {
      const m = await api(`/api/messages/${uid}?folder=${encodeURIComponent(folder)}&include_body=true&body_format=both`);
      state.current = m;
      state.showExternal = false;
      renderReader(m);
      if (m.unread) {
        fetch(`/api/messages/${uid}/flags`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ folder: folder, add: ["\\Seen"] }),
        }).then(() => {
          if (row) row.classList.remove("is-unread");
          loadFolders();
        }).catch(() => {});
      }
    } catch (e) {
      reader.innerHTML = "";
      reader.appendChild(readerBackBar("返回列表"));
      reader.appendChild(el("div", "empty", `读取失败：${e.message}`));
    }
  }

  /* ------------------------------ 正文渲染：富文本 ------------------------------ */
  // 直接丢掉的标签（脚本 / 样式 / 外嵌对象 / 表单）
  const DROP_TAGS = [
    "SCRIPT", "STYLE", "NOSCRIPT", "IFRAME", "OBJECT", "EMBED", "LINK", "META", "BASE",
    "FORM", "INPUT", "BUTTON", "SELECT", "TEXTAREA", "SVG", "MATH", "APPLET",
    "FRAME", "FRAMESET", "AUDIO", "VIDEO", "SOURCE", "TRACK", "CANVAS", "MAP", "AREA", "TITLE",
  ];

  // 保留内容、只剥掉标签的白名单（邮件排版主要靠 table / 内联样式）
  const KEEP_TAGS = new Set([
    "A", "ABBR", "ADDRESS", "ARTICLE", "ASIDE", "B", "BIG", "BLOCKQUOTE", "BR", "CAPTION", "CENTER",
    "CODE", "COL", "COLGROUP", "DD", "DEL", "DETAILS", "DIV", "DL", "DT", "EM", "FIGURE", "FIGCAPTION",
    "FONT", "FOOTER", "H1", "H2", "H3", "H4", "H5", "H6", "HEADER", "HR", "I", "IMG", "INS", "KBD",
    "LABEL", "LI", "MAIN", "MARK", "NAV", "OL", "P", "PRE", "Q", "S", "SAMP", "SECTION", "SMALL",
    "SPAN", "STRIKE", "STRONG", "SUB", "SUMMARY", "SUP", "TABLE", "TBODY", "TD", "TFOOT", "TH",
    "THEAD", "TIME", "TR", "TT", "U", "UL", "VAR",
  ]);

  const ATTR_ALL = new Set([
    "style", "class", "id", "title", "lang", "dir", "role",
    "align", "valign", "colspan", "rowspan", "nowrap", "width", "height",
    "bgcolor", "border", "cellpadding", "cellspacing", "size", "color", "face",
    "cite", "datetime", "start", "type", "scope", "summary", "span",
  ]);
  const ATTR_BY_TAG = {
    A: new Set(["href", "target", "rel", "name"]),
    IMG: new Set(["src", "alt", "width", "height", "border", "align", "hspace", "vspace", "loading"]),
  };

  function sanitizeStyle(v) {
    let s = String(v || "");
    s = s.replace(/expression\s*\(/gi, "");
    s = s.replace(/javascript\s*:/gi, "");
    s = s.replace(/vbscript\s*:/gi, "");
    s = s.replace(/-moz-binding\s*:/gi, "");
    // 远程背景图属于追踪像素，直接去掉
    s = s.replace(/url\s*\(\s*['"]?\s*(?:https?:)?\/\/[^)]*\)/gi, "");
    // 防止邮件样式盖住整个界面
    s = s.replace(/position\s*:\s*(fixed|absolute)\s*;?/gi, "");
    s = s.replace(/z-index\s*:\s*[^;]*;?/gi, "");
    return s;
  }

  function sanitizeHtml(html) {
    const doc = new DOMParser().parseFromString(String(html || ""), "text/html");
    doc.querySelectorAll(DROP_TAGS.join(",")).forEach((n) => n.remove());

    const walk = (parent) => {
      Array.from(parent.children).forEach((child) => {
        const tag = child.tagName;
        if (!KEEP_TAGS.has(tag)) {
          // 不在白名单：保留文字内容，剥掉标签本身
          const frag = doc.createDocumentFragment();
          while (child.firstChild) frag.appendChild(child.firstChild);
          child.replaceWith(frag);
          return;
        }
        Array.from(child.attributes).forEach((a) => {
          const n = a.name.toLowerCase();
          if (n.indexOf("on") === 0) { child.removeAttribute(a.name); return; }
          const ok = (ATTR_BY_TAG[tag] && ATTR_BY_TAG[tag].has(n)) || ATTR_ALL.has(n);
          if (!ok) { child.removeAttribute(a.name); return; }
          if (n === "style") child.setAttribute("style", sanitizeStyle(a.value));
          if ((n === "href" || n === "src") && /^\s*(javascript|vbscript|data:text\/html)\s*:/i.test(a.value)) {
            child.removeAttribute(a.name);
          }
        });
        if (tag === "A") {
          child.setAttribute("target", "_blank");
          child.setAttribute("rel", "noopener noreferrer");
        }
        walk(child);
      });
    };
    walk(doc.body);
    return doc.body.innerHTML;
  }

  function cidMap(m) {
    const map = {};
    (m.inline_images || []).forEach((a) => {
      const cid = String(a.content_id || "").replace(/[<>]/g, "").toLowerCase();
      if (cid) {
        map[cid] = a;
        map[cid.split("@")[0]] = a;
        map[cid.split("@")[0].split("$")[0]] = a;
      }
      if (a.filename) map[String(a.filename).toLowerCase()] = a;
    });
    return map;
  }

  /** 纯文本引用块：> 前缀的连续行、「-----原始邮件-----」分隔头之后的内容，包成 blockquote 让 foldQuotes 能折叠 */
  function wrapTextQuotes(html) {
    const lines = html.split("\n");
    const out = [];
    let open = false, tillEnd = false;
    const close = () => { if (open) { out.push("</blockquote>"); open = false; } };
    for (const ln of lines) {
      const q = ln.match(/^\s*&gt; ?(.*)$/);
      const sep = !q && /^\s*-{3,}\s*(?:原始邮件|Original\s+Message)\s*-*$/.test(ln);
      if (!open && !q && !sep) { out.push(ln); continue; }
      if (sep) {
        if (!open) { out.push('<blockquote class="mail-quote">'); open = true; }
        out.push(ln);
        tillEnd = true;
        continue;
      }
      if (!open) { out.push('<blockquote class="mail-quote">'); open = true; }
      out.push(q ? q[1] : ln);
    }
    close();
    return out.join("\n");
  }

  /** 纯文本正文 -> HTML：把 [cid:xxx] 占位还原成真实图片，引用行包成 blockquote */
  function textToHtml(text, m) {
    const map = cidMap(m);
    let s = String(text || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
    s = s.replace(/\[\s*cid:([^\]\s]+)\s*\]/gi, (full, cid) => {
      const a = map[cid.toLowerCase()] || map[cid.split("@")[0].toLowerCase()];
      if (!a) return "";
      return `<img class="inline-img" src="${a.inline_url}" alt="${a.filename}">`;
    });
    s = s.replace(/(https?:\/\/[^\s<>"']+)/gi, '<a href="$1" target="_blank" rel="noopener noreferrer">$1</a>');
    s = wrapTextQuotes(s);
    return s.replace(/\n/g, "<br>")
      .replace(/<blockquote class="mail-quote"><br>/g, '<blockquote class="mail-quote">');
  }

  /** 外链图片默认屏蔽（防追踪），未匹配的 cid 也占位，可加载的本地 cid 直接显示 */
  function applyImagePolicy(root) {
    let blocked = 0;
    root.querySelectorAll("img").forEach((img) => {
      const src = img.getAttribute("src") || "";
      if (/^https?:/i.test(src)) {
        blocked++;
        if (state.showExternal) {
          img.setAttribute("referrerpolicy", "no-referrer");
        } else {
          const ph = el("span", "ext-img");
          ph.dataset.src = src;
          ph.title = "点击加载这张外部图片";
          ph.addEventListener("click", () => {
            const img = new Image();
            img.src = src;
            img.referrerPolicy = "no-referrer";
            img.className = "inline-img";
            img.loading = "lazy";
            ph.replaceWith(img);
          });
          img.replaceWith(ph);
        }
      } else if (/^cid:/i.test(src)) {
        // 后端没找到匹配的 cid（极少见：邮件引用了不在 multipart 里的图）
        const ph = el("span", "ext-img");
        ph.textContent = "🖼 引用了未随邮件发送的图片";
        img.replaceWith(ph);
      } else {
        img.classList.add("inline-img");
        img.setAttribute("loading", "lazy");
      }
    });
    return blocked;
  }

  function renderReader(m) {
    const reader = $("#reader");
    reader.innerHTML = "";
    reader.appendChild(readerBackBar("返回列表"));

    const hasHtml = !!(m.body_html && m.body_html.trim());
    const hasText = !!(m.body_text && m.body_text.trim());
    let mode = state.bodyMode;
    if (mode === "source" && !hasHtml) mode = "rich";

    const bar = el("div", "reader-bar");
    const seg = el("div", "seg");
    [
      ["rich", "富文本", true],
      ["text", "纯文本", hasText],
      ["source", "源码", hasHtml],
    ].forEach(([id, label, enabled]) => {
      const b = el("button", mode === id ? "active" : "", label);
      b.disabled = !enabled;
      if (!enabled) b.style.opacity = "0.35";
      b.onclick = () => { state.bodyMode = id; renderReader(m); };
      seg.appendChild(b);
    });
    bar.appendChild(seg);

    const extBtn = el("button", "btn", "");
    extBtn.style.display = "none";
    extBtn.onclick = () => { state.showExternal = !state.showExternal; renderReader(m); };
    bar.appendChild(extBtn);

    if (/draft|草稿/i.test(m.folder || "")) {   // 草稿箱里的邮件可以直接接着写
      const editBtn = el("button", "btn btn-primary", "✏ 继续编辑");
      editBtn.onclick = () => openCompose({
        to: msgAddrs(m, "to"),
        cc: msgAddrs(m, "cc"),
        subject: m.subject || "",
        body: m.body_text || "",
        draft: { folder: m.folder, uid: m.uid },
        reply: draftReplyOf(m),
        forward: draftForwardOf(m),
      });
      bar.appendChild(editBtn);
    }

    const replyBtn = el("button", "btn", "↩ 回复");
    replyBtn.onclick = () => composeReply(m, false);
    bar.appendChild(replyBtn);
    bar.appendChild(replyAllBtn(m));
    bar.appendChild(forwardBtn(m));

    const delBtn = el("button", "btn btn-danger", "🗑 删除");
    delBtn.onclick = () => deleteMessage(m.folder, m.uid);
    bar.appendChild(delBtn);

    const unreadBtn = el("button", "btn", m.unread ? "标记已读" : "标记未读");
    unreadBtn.onclick = () => toggleUnread(m, unreadBtn);
    bar.appendChild(unreadBtn);
    bar.appendChild(el("div", "spacer"));
    if (m.inline_count) bar.appendChild(el("span", "attach-size", `内嵌 ${m.inline_count} 张`));
    bar.appendChild(el("span", "attach-size", `UID ${m.uid} · ${fmtBytes(m.size)}`));
    reader.appendChild(bar);

    const inner = el("div", "reader-inner");
    inner.appendChild(el("h2", "reader-subject", m.subject || "(无主题)"));

    const meta = el("div", "reader-meta");
    const addRow = (label, value) => {
      if (!value) return;
      const r = el("div", "meta-row");
      r.appendChild(el("span", "meta-label", label));
      r.appendChild(el("span", "meta-value", value));
      meta.appendChild(r);
    };
    addRow("发件人", `${m.from.name || ""} <${m.from.email}>`);
    if (m.to && m.to.length) addRow("收件人", m.to.map((t) => `${t.name || ""} <${t.email}>`).join("、"));
    if (m.cc && m.cc.length) addRow("抄送", m.cc.map((t) => `${t.name || ""} <${t.email}>`).join("、"));
    addRow("时间", fmtFull(m.date));
    addRow("目录", folderLabel(m.folder));
    inner.appendChild(meta);

    // 只列真实附件；内嵌图片已经渲染在正文里，不再重复列出
    if (m.attachments && m.attachments.length) {
      const box = el("div", "attach-list");
      m.attachments.forEach((a) => {
        const chip = el("a", "attach-chip");
        chip.href = a.url;
        chip.target = "_blank";
        chip.rel = "noopener";
        chip.appendChild(el("span", null, `📎 ${a.filename}`));
        chip.appendChild(el("span", "attach-size", fmtBytes(a.size)));
        box.appendChild(chip);
      });
      inner.appendChild(box);
    } else if (m.inline_count) {
      inner.appendChild(el("div", "attach-note", `${m.inline_count} 张内嵌图片已显示在正文中`));
    }

    const body = el("div", "reader-body");
    if (mode === "rich") body.classList.add("rich");
    if (mode === "source") {
      const f = document.createElement("iframe");
      f.className = "body-html-frame";
      f.setAttribute("sandbox", "");
      f.srcdoc = m.body_html;
      body.appendChild(f);
    } else if (mode === "text") {
      const pre = el("pre", "body-text");
      pre.textContent = (m.body_text || "").trim() || "（此邮件没有纯文本正文）";
      body.appendChild(pre);
    } else if (hasHtml) {
      body.innerHTML = sanitizeHtml(m.body_html);
    } else {
      body.innerHTML = textToHtml(m.body_text, m);
    }

    if (mode === "rich") {
      const blocked = applyImagePolicy(body);
      if (blocked) {
        extBtn.style.display = "";
        extBtn.textContent = state.showExternal ? "隐藏外部图片" : `显示 ${blocked} 张外部图片`;
      }
    }

    inner.appendChild(body);
    reader.appendChild(inner);
    inner.scrollTop = 0;
  }

  async function toggleUnread(m, btn) {
    try {
      const r = await api(`/api/messages/${m.uid}/flags`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ folder: m.folder, add: m.unread ? ["\\Seen"] : [], remove: m.unread ? [] : ["\\Seen"] }),
      });
      m.unread = r.unread;
      btn.textContent = m.unread ? "标记已读" : "标记未读";
      loadFolders();
      loadMessages(false);
    } catch (e) { toast("操作失败：" + e.message); }
  }

  /* ------------------------------ 会话视图 ------------------------------ */
  function currentParams(extra) {
    const params = new URLSearchParams({
      since: state.since === "all" ? "" : state.since,
      limit: String(state.limit),
      offset: String(state.offset),
    });
    if (state.q) params.set("q", state.q);
    if (state.unreadOnly) params.set("unread_only", "true");
    if (state.category === "boring") params.set("boring", "true");
    else if (state.category === "personal") params.set("boring", "false");
    else if (state.category !== "all") params.set("category", state.category);
    if (state.since === "all") params.delete("since");
    if (extra) extra(params);
    return params;
  }

  async function loadThreads(append) {
    const list = $("#messageList");
    if (!append) { list.innerHTML = '<div class="empty">加载中…</div>'; state.offset = 0; }
    const params = currentParams((p) => p.set("folder", state.folder));
    let data;
    try {
      data = await api("/api/threads?" + params.toString());
    } catch (e) {
      list.innerHTML = `<div class="empty">加载失败：${e.message}</div>`;
      return;
    }
    state.total = data.total;
    state.items = append ? state.items.concat(data.items) : data.items;

    list.innerHTML = "";
    if (!state.items.length) {
      renderEmptyList("会话");
      $("#listCount").textContent = "0 条";
      $("#moreBtn").style.display = "none";
      return;
    }
    state.items.forEach((t) => {
      const row = el("div", "thread-row" + (t.unread ? " is-unread" : "") + (t.is_boring ? " is-boring" : ""));
      row.dataset.thread = t.thread_id;

      const av = el("div", "avatar", t.is_boring ? CAT_META[t.category].emoji : initials(t.last_from.name, t.last_from.email));
      row.appendChild(av);

      const main = el("div", "thread-main");
      const line1 = el("div", "thread-line1");
      line1.appendChild(el("span", "thread-title", t.subject));
      line1.appendChild(el("span", "thread-time", fmtTime(t.last_date)));
      main.appendChild(line1);

      const line2 = el("div", "thread-line2");
      const names = (t.participants || []).map((p) => p.name).slice(0, 3).join("、");
      line2.appendChild(el("span", "thread-people", names + (t.participant_count > 3 ? ` 等 ${t.participant_count} 人` : "")));
      if (t.message_count > 1) line2.appendChild(el("span", "thread-count", t.message_count));
      const badge = catBadge(t);
      if (badge) line2.appendChild(badge);
      main.appendChild(line2);

      main.appendChild(el("div", "thread-snippet", t.snippet || ""));
      row.appendChild(main);
      if (t.unread) row.appendChild(el("span", "badge-unread"));

      row.addEventListener("click", () => openThread(t.thread_id, row));
      list.appendChild(row);
    });

    $("#listCount").textContent = `${state.total} 条会话`;
    $("#moreBtn").style.display = state.items.length < state.total ? "" : "none";
    $("#moreBtn").textContent = `加载更多（还有 ${state.total - state.items.length} 条）`;
  }

  async function openThread(threadId, row) {
    document.querySelectorAll(".thread-row").forEach((n) => n.classList.remove("is-selected"));
    if (row) row.classList.add("is-selected");

    const reader = $("#reader");
    reader.innerHTML = '<div class="empty">加载中…</div>';
    setReading(true);
    try {
      const t = await api(`/api/threads/${threadId}?body_chars=60000&body_format=both`);
      renderChat(t);
      // 把未读的都顺手标已读（和点开单封一致）
      const unreadMsgs = (t.messages || []).filter((m) => m.unread && m.folder !== "Drafts");
      if (unreadMsgs.length) {
        Promise.all(
          unreadMsgs.map((m) =>
            fetch(`/api/messages/${m.uid}/flags`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ folder: m.folder, add: ["\\Seen"] }),
            }).catch(() => {})
          )
        ).then(() => {
          if (row) { row.classList.remove("is-unread"); }
          loadFolders();
        });
      }
    } catch (e) {
      reader.innerHTML = "";
      reader.appendChild(readerBackBar("返回列表"));
      reader.appendChild(el("div", "empty", `读取失败：${e.message}`));
    }
  }

  /** 把长引用折叠起来（聊天感觉的关键） */
  function foldQuotes(root) {
    root.querySelectorAll("blockquote").forEach((q) => {
      if ((q.textContent || "").trim().length < 120) return;
      const det = document.createElement("details");
      det.className = "quote-fold";
      const sum = el("summary", null, "⋯ 显示引用的历史内容");
      det.appendChild(sum);
      q.replaceWith(det);
      det.appendChild(q);
    });
    // 常见的「-----原始邮件-----」分隔块（div 形式）
    root.querySelectorAll("div").forEach((d) => {
      if (d.closest(".quote-fold")) return;
      const head = (d.textContent || "").trim().slice(0, 60);
      if (!/^(?:[-=_]{3,}\s*)?(?:原始邮件|Original Message|发件人[:：]|From\s*:)/i.test(head)) return;
      if ((d.textContent || "").trim().length < 120) return;
      const det = document.createElement("details");
      det.className = "quote-fold";
      det.appendChild(el("summary", null, "⋯ 显示引用的历史内容"));
      d.replaceWith(det);
      det.appendChild(d);
    });
  }

  function renderChat(t) {
    const reader = $("#reader");
    reader.innerHTML = "";
    reader.appendChild(readerBackBar("返回列表"));

    const bar = el("div", "reader-bar");
    bar.appendChild(el("span", "chat-stat", `${t.message_count} 封 · ${t.participant_count} 人 · ${t.unread ? t.unread + " 未读" : "全部已读"}`));
    bar.appendChild(el("div", "spacer"));
    if (t.is_boring) {
      const meta = CAT_META[t.category] || { emoji: "❓", label: t.category_label };
      bar.appendChild(el("span", "cat-badge " + (CAT_META[t.category] ? CAT_META[t.category].cls : ""), `${meta.emoji} ${meta.label} · 无聊邮件`));
    }
    const last = (t.messages || [])[t.messages.length - 1];
    const backBtn = el("button", "btn", "在邮件视图打开最新一封");
    backBtn.disabled = !last;
    backBtn.onclick = () => {
      if (!last) return;
      state.viewMode = "message";
      document.querySelectorAll("#viewSeg button").forEach((b) => b.classList.toggle("active", b.dataset.view === "message"));
      loadMessages(false).then(() => {
        const row = document.querySelector(`.msg-row[data-uid="${last.uid}"]`);
        openMessage(last.folder, last.uid, row);
      });
    };
    const lastIn = [...(t.messages || [])].reverse().find((m) => !m.mine) || last;
    if (lastIn) {
      const replyBtn = el("button", "btn", "↩ 回复");
      replyBtn.onclick = () => composeReply(lastIn, false);
      bar.appendChild(replyBtn);
      bar.appendChild(replyAllBtn(lastIn));
      bar.appendChild(forwardBtn(lastIn));
    }
    bar.appendChild(backBtn);
    reader.appendChild(bar);

    const inner = el("div", "reader-inner chat-wrap");
    inner.appendChild(el("h2", "reader-subject", t.subject || "(无主题)"));

    const meta = el("div", "reader-meta chat-meta");
    const r = el("div", "meta-row");
    r.appendChild(el("span", "meta-label", "参与人"));
    r.appendChild(el("span", "meta-value", (t.participants || []).map((p) => p.name || p.email).join("、") || "-"));
    meta.appendChild(r);
    inner.appendChild(meta);

    const chat = el("div", "chat");
    let lastDay = "";
    let prev = null;

    t.messages.forEach((m) => {
      const d = parseDate(m.date);
      const day = d ? d.toDateString() : "";
      if (day && day !== lastDay) {
        chat.appendChild(el("div", "chat-day", groupLabel(m.date)));
        lastDay = day;
        prev = null;
      }

      const mine = !!m.mine;
      const sender = m.from || {};
      const nearPrev =
        prev && prev.mine === mine &&
        (prev.from || {}).email === sender.email &&
        d && prev._d && d - prev._d < 10 * 60000;

      const rowEl = el("div", "bubble-row" + (mine ? " mine" : ""));
      const av = el("div", "avatar", initials(sender.name, sender.email));
      if (av) rowEl.appendChild(av);

      const bubble = el("div", "bubble" + (mine ? " mine" : "") + (nearPrev ? " grouped" : ""));
      if (!nearPrev) {
        const head = el("div", "bubble-head");
        head.appendChild(el("span", "bubble-name", sender.name || sender.email || "?"));
        head.appendChild(el("span", "bubble-time", fmtFull(m.date)));
        const badge = catBadge(m);
        if (badge) head.appendChild(badge);
        bubble.appendChild(head);
      }

      const body = el("div", "bubble-body");
      if (m.body_html && m.body_html.trim()) {
        body.innerHTML = sanitizeHtml(m.body_html);
      } else {
        body.innerHTML = textToHtml(m.body_text, m);
      }
      foldQuotes(body);
      const blocked = applyImagePolicy(body);
      bubble.appendChild(body);

      if (m.attachments && m.attachments.length) {
        const box = el("div", "attach-list bubble-attach");
        m.attachments.forEach((a) => {
          const chip = el("a", "attach-chip");
          chip.href = a.url;
          chip.target = "_blank";
          chip.rel = "noopener";
          chip.appendChild(el("span", null, `📎 ${a.filename}`));
          chip.appendChild(el("span", "attach-size", fmtBytes(a.size)));
          box.appendChild(chip);
        });
        bubble.appendChild(box);
      }

      const foot = el("div", "bubble-foot");
      foot.appendChild(el("span", "bubble-folder", folderLabel(m.folder)));
      foot.appendChild(el("span", "bubble-size", `UID ${m.uid}`));
      const rBtn = el("button", "bubble-open", "↩ 回复");
      rBtn.onclick = () => composeReply(m, false);
      foot.appendChild(rBtn);
      foot.appendChild(replyAllBtn(m, "bubble-open"));
      foot.appendChild(forwardBtn(m, "bubble-open"));
      const dBtn = el("button", "bubble-open bubble-del", "🗑 删除");
      dBtn.onclick = () => deleteMessage(m.folder, m.uid);
      foot.appendChild(dBtn);
      const openBtn = el("button", "bubble-open", "查看原文 ⤢");
      openBtn.onclick = () => {
        state.viewMode = "message";
        document.querySelectorAll("#viewSeg button").forEach((b) => b.classList.toggle("active", b.dataset.view === "message"));
        loadMessages(false).then(() => {
          const row = document.querySelector(`.msg-row[data-uid="${m.uid}"]`);
          openMessage(m.folder, m.uid, row);
        });
      };
      foot.appendChild(openBtn);
      bubble.appendChild(foot);

      rowEl.appendChild(bubble);
      chat.appendChild(rowEl);
      prev = { mine, from: sender, _d: d };
    });

    inner.appendChild(chat);
    reader.appendChild(inner);
    inner.scrollTop = inner.scrollHeight;
  }

  /* ------------------------------ 收件人 / 抄送：chip 输入框 ------------------------------
     一个收件人就是一个「框」，输入时按历史邮件做相近联想。
     内部只维护 values = [{name, email}]，提交时再序列化成 "Name <a@b.com>, ..."，
     所以后端 /api/send 与 /api/drafts 的 to / cc 字段依然是普通的逗号分隔字符串。 */

  const CHIP_SEP_RE = /[,;，；\r\n\t]+/;   // 分隔符：中英文逗号 / 分号 / 换行
  // 显示名里出现这些字符时必须加引号（和 Python 的 email.utils.formataddr 同款规则）：
  // 不加的话 `Family, Given <first.sample@example.com>` 会被收发两端都当成「两个人」——
  // 「Family」和「Steven」各算一个收件人，SMTP 拿 Family 去投，服务器回 550，
  // 前端只看到一个 400「部分收件人被拒」，跟「收件人不见了」没什么区别。
  const CHIP_SPECIAL_RE = /["(),:;<>@[\]\\]/;

  /** 'Zhang San <w@x.com>' / 'w@x.com' -> {name, email}；不是地址则返回 null。 */
  function parseAddr(text) {
    let s = String(text == null ? "" : text).trim();
    if (!s) return null;
    const m = s.match(/^(.*?)\s*[<＜]\s*([^>＞]+?)\s*[>＞]\s*$/);
    if (m) {
      const name = m[1].trim().replace(/^["']|["']$/g, "");
      const email = m[2].trim();
      return email.includes("@") ? { name, email } : null;
    }
    s = s.replace(/^["']|["']$/g, "");
    return s.includes("@") ? { name: "", email: s } : null;
  }

  /** 按分隔符切成一个个地址；引号里的逗号不算分隔符（"Family, Given" <a@b.c> 是一个人）。 */
  function splitAddrList(text) {
    const out = [];
    let buf = "";
    let quoted = false;
    for (const ch of String(text == null ? "" : text)) {
      if (ch === '"') { quoted = !quoted; buf += ch; continue; }
      if (!quoted && CHIP_SEP_RE.test(ch)) {
        if (buf.trim()) out.push(buf.trim());
        buf = "";
        continue;
      }
      buf += ch;
    }
    if (buf.trim()) out.push(buf.trim());
    return out;
  }

  /** {name, email} -> 报文里那个字符串（显示名带特殊字符就加引号）。 */
  function formatAddr(v) {
    const email = (v && v.email) || "";
    const name = (v && v.name) || "";
    if (!name || name === email) return email;
    const nm = CHIP_SPECIAL_RE.test(name) ? `"${name.replace(/"/g, '\\"')}"` : name;
    return `${nm} <${email}>`;
  }

  /** 把一份地址列表（字符串 / 数组 / 通讯录对象）规整成 {@name,@email} 并去重。 */
  function normAddrs(list) {
    if (typeof list === "string") list = splitAddrList(list);
    const out = [];
    const seen = new Set();
    (list || []).forEach((item) => {
      let v = null;
      if (typeof item === "string") v = parseAddr(item);
      else if (item && item.email) v = { name: item.name || "", email: String(item.email).trim() };
      if (!v || !v.email) return;
      const k = v.email.toLowerCase();
      if (seen.has(k)) return;
      seen.add(k);
      out.push(v);
    });
    return out;
  }

  /** 造一个 chip 输入框。root/menu/chips/input 是 index.html 里已有的四个节点。 */
  function createChipField(ids, label) {
    const field = $(ids.field);
    const menu = $(ids.menu);
    const chipBox = $(ids.chips);
    const input = $(ids.input);
    let values = [];        // 已确认的收件人
    let options = [];       // 当前联想候选
    let active = -1;
    let seq = 0;            // 请求序号：只认最后一次返回
    let timer = null;

    const has = (email) => values.some((v) => v.email.toLowerCase() === String(email || "").toLowerCase());

    function render() {
      chipBox.innerHTML = "";
      values.forEach((v, i) => {
        const c = el("span", "chip");
        c.title = v.name ? `${v.name} <${v.email}>` : v.email;
        if (v.name) c.appendChild(el("span", "chip-name", v.name));
        c.appendChild(el("span", "chip-addr", v.name ? `<${v.email}>` : v.email));
        const x = el("button", "chip-x", "×");
        x.type = "button";
        x.title = "移除";
        x.onmousedown = (e) => e.preventDefault();
        x.onclick = () => { values.splice(i, 1); render(); markComposeDirty(); input.focus(); };
        c.appendChild(x);
        chipBox.appendChild(c);
      });
    }

    function setBad(on) {
      field.classList.toggle("is-bad", !!on);
      if (on) setTimeout(() => field.classList.remove("is-bad"), 1600);
    }

    /** 收下一个收件人。返回 "ok" | "dup"（已经在框里）| "invalid"（没地址）。 */
    function add(v) {
      if (!v || !v.email) return "invalid";
      if (has(v.email)) { toast(`「${v.email}」已在${label}里`, 2200); return "dup"; }
      values.push({ name: v.name || "", email: v.email });
      render();
      markComposeDirty();
      return "ok";
    }

    function closeMenu() {
      menu.hidden = true;
      menu.innerHTML = "";
      options = [];
      active = -1;
    }

    function highlight() {
      [...menu.querySelectorAll(".chip-opt")].forEach((n, i) => n.classList.toggle("is-active", i === active));
    }

    function drawMenu() {
      menu.innerHTML = "";
      if (!options.length) {
        const q = input.value.trim();
        menu.appendChild(el("div", "chip-menu-hint",
          q ? `没有匹配「${q}」的联系人 · 输入完整邮箱后按回车可直接填入`
            : `还没有可推荐的联系人（同步邮件后就能按姓名/邮箱联想了）`));
        menu.hidden = false;
        return;
      }
      options.forEach((o, i) => {
        const row = el("div", "chip-opt" + (i === active ? " is-active" : ""));
        row.appendChild(el("span", "chip-opt-name", o.name || o.email.split("@")[0]));
        row.appendChild(el("span", "chip-opt-mail", o.name ? o.email : ""));
        if (o.count) row.appendChild(el("span", "chip-opt-meta", `${o.count} 封`));
        row.onmousedown = (e) => { e.preventDefault(); pick(i); };
        row.onmouseenter = () => { active = i; highlight(); };
        menu.appendChild(row);
      });
      menu.hidden = false;
    }

    function pick(i) {
      const o = options[i];
      if (!o) return false;
      input.value = "";
      add(o);
      closeMenu();
      input.focus();
      return true;
    }

    async function refresh() {
      const q = input.value.trim();
      const my = ++seq;
      try {
        const r = await api(`/api/contacts?q=${encodeURIComponent(q)}&limit=7`);
        if (my !== seq) return;
        options = (r.items || []).filter((o) => !has(o.email));
        active = options.length ? 0 : -1;
        drawMenu();
      } catch (e) {
        if (my !== seq) return;
        closeMenu();      // 联想服务挂了也不影响手输地址
      }
    }

    function scheduleRefresh() {
      clearTimeout(timer);
      timer = setTimeout(refresh, 160);
    }

    /** 把输入框里还没确认的文字变成 chip；返回是否吃掉了内容。 */
    function commit() {
      const raw = input.value.trim();
      if (!raw) return false;
      const parts = splitAddrList(raw);
      let added = 0;
      let bad = "";
      parts.forEach((p) => {
        let v = parseAddr(p);
        if (!v) {
          // 只输了名字没输邮箱：拿当前候选里同名的顶上（唯一候选也认）
          const low = p.toLowerCase();
          const hit = options.find((o) => (o.name || "").toLowerCase() === low)
            || (options.length === 1 ? options[0] : null);
          if (hit) v = { name: hit.name || "", email: hit.email };
        }
        if (!v) { bad = bad || p; return; }
        const r = add(v);
        if (r === "ok") added++;
        else if (r === "invalid") bad = bad || p;   // 解析出来了却没有地址（候选缺 email），原文留着别吃
      });
      if (added) input.value = bad ? bad : "";
      if (added) closeMenu();
      else if (bad) setBad(true);
      else input.value = "";        // 只剩分隔符（或本来就在框里），清掉
      return added > 0;
    }

    input.addEventListener("keydown", (e) => {
      if (e.isComposing) return;                      // 中文输入法组词中，回车不是确认
      const k = e.key;
      if (k === "Enter") {
        e.preventDefault();
        const typed = input.value.trim();
        if (typed.includes("@") && parseAddr(typed)) { commit(); return; }
        if (!menu.hidden && active >= 0 && options[active]) { pick(active); return; }
        commit();
        return;
      }
      if (k === "," || k === ";" || k === "，" || k === "；") {
        e.preventDefault();
        commit();
        return;
      }
      if (k === "Backspace" && !input.value && values.length) {
        values.pop();
        render();
        markComposeDirty();
        return;
      }
      if (k === "ArrowDown" || k === "ArrowUp") {
        e.preventDefault();
        if (menu.hidden) { refresh(); return; }
        if (!options.length) return;
        active = k === "ArrowDown"
          ? (active + 1) % options.length
          : (active <= 0 ? options.length - 1 : active - 1);
        highlight();
        return;
      }
      if (k === "Escape") {
        if (!menu.hidden) { e.stopPropagation(); closeMenu(); }
      }
    });

    input.addEventListener("input", scheduleRefresh);
    input.addEventListener("focus", refresh);
    input.addEventListener("blur", () => {
      commit();
      closeMenu();
    });
    input.addEventListener("paste", (e) => {
      const txt = (e.clipboardData || window.clipboardData).getData("text") || "";
      if (!txt || !CHIP_SEP_RE.test(txt)) return;      // 单个地址走默认粘贴
      e.preventDefault();
      normAddrs(txt).forEach(add);
    });
    field.addEventListener("mousedown", (e) => {
      if (e.target === field) { e.preventDefault(); input.focus(); }
    });

    return {
      setValues: (list) => { values = normAddrs(list); render(); },
      serialize: () => values.map(formatAddr).join(", "),
      flush: commit,
      isEmpty: () => values.length === 0,
      count: () => values.length,
      focus: () => input.focus(),
    };
  }

  /* ------------------------------ 写信纯逻辑（可单测） ------------------------------ */

  /** 附件的身份：同名 + 同大小 + 同修改时间，视为同一个文件。 */
  function fileKey(f) {
    return [f && f.name, f && f.size, f && f.lastModified].join("|");
  }

  /** 往附件列表里追加，重复的不加；返回真正新增的数量（原地改 list）。 */
  function appendFiles(list, files) {
    const seen = new Set(list.map(fileKey));
    let added = 0;
    for (const f of files || []) {
      const k = fileKey(f);
      if (seen.has(k)) continue;
      seen.add(k);
      list.push(f);
      added++;
    }
    return added;
  }

  function removeFileAt(list, index) {
    if (!(index >= 0 && index < list.length)) return false;
    list.splice(index, 1);
    return true;
  }

  function sumBytes(list) {
    return (list || []).reduce((n, f) => n + (f && f.size ? f.size : 0), 0);
  }

  /** 套用 AI 方案：正文一定替换；只有 withSubject 为真且方案带了主题时才动主题。 */
  function applyAiOption(fields, opt, withSubject) {
    const cur = fields || {};
    const sub = (opt && opt.subject) || "";
    const useSubject = !!(withSubject && sub);
    return {
      body: (opt && opt.body) || "",
      subject: useSubject ? sub : (cur.subject || ""),
      subjectUsed: useSubject,
    };
  }

  /**
   * 存草稿时要一起带上的「非正文」上下文：顶掉哪一版、以及这封本来是回复谁 / 转发哪封。
   * 回复 / 转发上下文必须落进草稿（后端存进 X-Message-CLI-Reply-* / -Forward-* 头），
   * 否则关掉写信框、从草稿箱再打开时上下文就丢了 —— 那封"回复"会作为新邮件发出去
   * （对方另起一个线程），"转发"会丢掉引用块归属。
   */
  function draftContextFields(draft, reply, forward) {
    return {
      draft_folder: draft ? draft.folder : "",
      draft_uid: draft ? draft.uid : 0,
      reply_folder: reply ? reply.folder : "",
      reply_uid: reply ? reply.uid : 0,
      forward_folder: forward ? forward.folder : "",
      forward_uid: forward ? forward.uid : 0,
    };
  }

  /* ------------------------------ 写邮件 / 删除 ------------------------------ */

  function composeFields() {
    // 输入框里还没回车的地址先转成 chip，免得「输了一半就点发送」丢人
    if (chips.to) chips.to.flush();
    if (chips.cc) chips.cc.flush();
    return {
      to: chips.to ? chips.to.serialize() : "",
      cc: chips.cc ? chips.cc.serialize() : "",
      subject: $("#cSubject").value.trim(),
      body: $("#cBody").value,
    };
  }

  function composeHasContent(f) {
    return !!(f.to || f.cc || f.subject || (f.body || "").trim());
  }

  function setComposeStatus(text, ok) {
    const s = $("#composeStatus");
    s.textContent = text || "";
    s.classList.toggle("is-ok", !!ok);
  }

  function setDraftStatus(text) { $("#draftStatus").textContent = text || ""; }

  /* ---- 附件：自己维护一份 File 列表，才能一个个移除 ---- */
  function paintFileList() {
    const box = $("#cFileList");
    const files = state.compose.files;
    box.innerHTML = "";
    box.hidden = !files.length;
    files.forEach((f, i) => {
      const item = el("div", "file-item");
      item.appendChild(el("span", "file-item-name", f.name));
      item.appendChild(el("span", "file-item-size", fmtBytes(f.size)));
      const x = el("button", "file-item-x", "×");
      x.type = "button";
      x.title = "移除这个附件";
      x.setAttribute("aria-label", `移除附件 ${f.name}`);
      x.onclick = () => {
        removeFileAt(files, i);
        paintFileList();
        markComposeDirty();
      };
      item.appendChild(x);
      box.appendChild(item);
    });
    $("#cFileNames").textContent = files.length
      ? `共 ${files.length} 个 · ${fmtBytes(sumBytes(files))}`
      : "";
    $("#cFiles").value = "";   // 清空 input，才能再次选中同一个文件
  }

  function addComposeFiles(list) {
    const n = appendFiles(state.compose.files, list);
    paintFileList();
    if (n) {
      markComposeDirty();
      toast(`已添加 ${n} 个附件`, 1800);
    } else if (list && list.length) {
      toast("这些附件已经在列表里了", 1800);
    }
    return n;
  }

  /* ---- AI 面板：收起后结果留着，点按钮还能再展开 ---- */
  function setAiPanel(open) {
    const opts = (state.compose.ai && state.compose.ai.options) || [];
    const has = opts.length > 0;
    $("#aiOpts").hidden = !(has && open);
    $("#aiToggle").hidden = !(has && !open);
    $("#aiToggle").textContent = has ? `✨ AI 建议 · ${opts.length}` : "✨ AI 建议";
  }

  /* ---- 草稿自动保存：停止输入 1.8s 存一次；关窗/关页面立刻存 ---- */
  function markComposeDirty() {
    state.compose.dirty = true;
    clearTimeout(state.compose.timer);
    state.compose.timer = setTimeout(() => { saveDraft(); }, DRAFT_DEBOUNCE);
  }

  /** 草稿要一起带上的「非正文」上下文：顶掉哪一版、以及这封本来是回复谁 / 转发哪封。 */
  function draftContextPayload() {
    return draftContextFields(state.compose.draft, state.compose.reply, state.compose.forward);
  }

  async function saveDraft(force) {
    if (state.compose.sending) return null;   // 正在发送，别再存草稿（否则发完会剩一版）
    const f = composeFields();
    if (!composeHasContent(f)) return null;
    if (!force && !state.compose.dirty) return state.compose.draft;
    clearTimeout(state.compose.timer);
    state.compose.dirty = false;
    const d = state.compose.draft;
    // 存成「可等待的一次任务」挂在 state 上：发送时必须先等它回来，
    // 才知道这次自动保存最终落到了哪个 uid（否则它会晚于发送登记，多留一版草稿）。
    const job = (async () => {
      try {
        const r = await jsonPost("/api/drafts", Object.assign({}, f, draftContextPayload()));
        state.compose.draft = { folder: r.folder, uid: r.uid };
        setDraftStatus(`草稿已存「${folderLabel(r.folder)}」 ${r.saved_at}`);
        if (state.folder === r.folder && state.viewMode === "message") loadCurrent(false);
        return state.compose.draft;
      } catch (err) {
        state.compose.dirty = true;   // 存失败就留着标记，下次再试
        setDraftStatus("草稿保存失败：" + err.message);
        return null;
      }
    })();
    state.compose.saving = job;
    try {
      return await job;
    } finally {
      if (state.compose.saving === job) state.compose.saving = null;
    }
  }

  function draftPayloadForBeacon() {
    const f = composeFields();
    return Object.assign({}, f, draftContextPayload());
  }

  function openCompose(prefill) {
    prefill = prefill || {};
    clearTimeout(state.compose.timer);
    state.compose.seq = (state.compose.seq || 0) + 1;
    state.compose.reply = prefill.reply || null;
    state.compose.forward = prefill.forward || null;
    state.compose.draft = prefill.draft || null;
    state.compose.dirty = false;
    state.compose.saving = null;
    state.compose.sending = false;
    state.compose.files = [];
    state.compose.ai = null;
    const what = state.compose.reply ? "回复" : (state.compose.forward ? "转发" : "");
    $("#composeTitle").textContent = state.compose.draft
      ? (what ? `编辑草稿（${what}）` : "编辑草稿")
      : (what ? `${what}邮件` : "写邮件");
    chips.to.setValues(prefill.to || "");
    chips.cc.setValues(prefill.cc || "");
    $("#cTo").value = "";
    $("#cCc").value = "";
    $("#cSubject").value = prefill.subject || "";
    $("#cBody").value = prefill.body || "";
    paintFileList();
    setComposeStatus("");
    setDraftStatus(state.compose.draft ? `正在编辑草稿箱里的这一版（保存会覆盖它）` : "");
    setAiPanel(false);
    $("#composeModal").hidden = false;
    setTimeout(() => chips.to.focus(), 60);
  }

  /** 关窗：先把没存的内容落进草稿箱，再收起 —— 误关/拖选误关都不会丢内容。 */
  function closeCompose() {
    const f = composeFields();
    if (state.compose.dirty && composeHasContent(f)) {
      saveDraft(true).then((d) => {
        if (d) toast(`已存入「${folderLabel(d.folder)}」`, 2600);
      });
    } else if (state.compose.files.length) {
      // 草稿接口不存附件，得提前说一声，免得下次打开发现附件没了
      toast(`有 ${state.compose.files.length} 个附件：草稿存不了附件，下次请重新添加`, 4000);
    }
    clearTimeout(state.compose.timer);
    $("#aiOpts").hidden = true;
    $("#aiToggle").hidden = true;
    $("#composeModal").hidden = true;
  }

  function replyTargetOf(m) {
    // 单个「回复」：自己发的信 -> 回给原收件人；别人发的 -> 回给发件人
    if (m.mine || m.folder === "Sent Items" || m.folder === "Drafts") return msgAddrs(m, "to");
    const from = m.from || {};
    return from.email ? [{ name: from.name || "", email: from.email }] : [];
  }

  /** 草稿里记着的「本来要回复谁」（后端存草稿时写进 X-Message-CLI-Reply-* 头）。 */
  function draftReplyOf(m) {
    const x = (m && m.headers && m.headers.x_reply) || null;
    return x && x.folder && x.uid ? { folder: x.folder, uid: x.uid } : null;
  }

  /** 草稿里记着的「本来在转发哪封」（X-Message-CLI-Forward-* 头）。 */
  function draftForwardOf(m) {
    const x = (m && m.headers && m.headers.x_forward) || null;
    return x && x.folder && x.uid ? { folder: x.folder, uid: x.uid } : null;
  }

  /** 收件人/抄送字段规整成 [{name, email}]，丢掉空值和重复。 */
  function msgAddrs(m, which) {
    return normAddrs(((m && m[which]) || []).map((p) => ({ name: p.name || "", email: p.email || "" })));
  }

  /** 「回复全部」：发件人 + 原收件人 + 原抄送，去掉自己和重复。 */
  function replyAllTargets(m) {
    const me = myAddr();
    const from = m.from || {};
    const head = (m.mine || !from.email) ? [] : [{ name: from.name || "", email: from.email }];
    const to = normAddrs(head.concat(msgAddrs(m, "to"))).filter((p) => p.email.toLowerCase() !== me);
    const toSet = new Set(to.map((p) => p.email.toLowerCase()));
    const cc = msgAddrs(m, "cc").filter((p) => p.email.toLowerCase() !== me && !toSet.has(p.email.toLowerCase()));
    return { to, cc };
  }

  /** 自己的地址：优先用 /api/status 拿到的，没到就退回顶栏显示的文字。 */
  function myAddr() {
    if (state.me) return state.me.toLowerCase();
    const t = (($("#account") || {}).textContent || "").trim();
    return /@/.test(t) ? t.toLowerCase() : "";
  }

  function composeReply(m, all) {
    const t = all ? replyAllTargets(m) : { to: replyTargetOf(m), cc: [] };
    openCompose({
      to: t.to,
      cc: t.cc,
      subject: /^re:/i.test(m.subject || "") ? m.subject : "Re: " + (m.subject || ""),
      reply: { folder: m.folder, uid: m.uid },
    });
  }

  /** 转发的主题：已经有了 Fwd: / FW: 就不再叠一层。 */
  function forwardSubject(subject) {
    const s = (subject || "").trim();
    return /^(?:fwd?|fw)\s*:/i.test(s) ? s : "Fwd: " + s;
  }

  /**
   * 转发：收件人留空等人填，主题加 Fwd:，正文由后端把原信作为引用块拼在下面。
   * 原邮件的真实附件一并带上 —— 转发一封带附件的信却不带附件，等于只转了一半。
   * （内嵌图不算：它们在引用块里作为 cid 图片随信发出，见 app/quoting.py。）
   */
  function composeForward(m) {
    openCompose({
      to: [],
      cc: [],
      subject: forwardSubject(m.subject),
      forward: { folder: m.folder, uid: m.uid },
    });
    const atts = (m.attachments || []).filter((a) => a && a.url && !a.is_inline);
    if (atts.length) attachOriginalFiles(atts, state.compose.seq);
  }

  /** 自动带上原邮件的附件：逐个取回来变成写信框里的 File。 */
  const FORWARD_ATTACH_MAX = 25 * 1024 * 1024;   // 合计上限，超了就说一声让用户自己挑

  async function attachOriginalFiles(atts, seq) {
    const files = [];
    let bytes = 0;
    let skipped = 0;
    for (const a of atts) {
      if (bytes + (a.size || 0) > FORWARD_ATTACH_MAX) { skipped++; continue; }
      try {
        const r = await fetch(a.url);
        if (!r.ok) throw new Error(String(r.status));
        const blob = await r.blob();
        if (bytes + blob.size > FORWARD_ATTACH_MAX) { skipped++; continue; }
        bytes += blob.size;
        files.push(new File([blob], a.filename || "attachment.bin", {
          type: a.content_type || blob.type || "application/octet-stream",
        }));
      } catch (e) {
        skipped++;
      }
    }
    if (seq !== state.compose.seq) return;    // 窗口已经换过一轮了，别往里塞
    const added = appendFiles(state.compose.files, files);
    paintFileList();
    if (added && skipped) toast(`已带上原邮件附件 ${added} 个（另有 ${skipped} 个太大或取不到，请手动添加）`, 4200);
    else if (added) toast(`已带上原邮件附件 ${added} 个`, 2600);
    else if (skipped) toast(`原邮件的 ${skipped} 个附件没能带上（太大或取不到），请手动添加`, 4200);
  }

  /** 转发按钮：哪封都能转，所以永远可点（除非这封信连 folder/uid 都没有）。 */
  function forwardBtn(m, cls) {
    const b = el("button", cls || "btn", "↪ 转发");
    b.title = "把原邮件作为引用块转到别处（附件一并带上）";
    b.disabled = !(m && m.folder && m.uid);
    if (b.disabled) b.style.opacity = "0.4";
    b.onclick = () => composeForward(m);
    return b;
  }

  /** 回复全部按钮：原邮件除了自己没别人时置灰（那和普通回复没区别）。 */
  function replyAllBtn(m, cls) {
    const t = replyAllTargets(m);
    const n = t.to.length + t.cc.length;
    const b = el("button", cls || "btn", "↩↩ 回复全部");
    b.title = n > 1 ? `发件人 + 收件人 + 抄送，共 ${n} 人` : "原邮件没有其他收件人";
    b.disabled = n <= 1;
    if (b.disabled) b.style.opacity = "0.4";
    b.onclick = () => composeReply(m, true);
    return b;
  }

  /* ------------------------------ AI 写正文 ------------------------------ */

  function renderAIOptions(r) {
    state.compose.ai = r;
    const box = $("#aiOpts");
    box.innerHTML = "";

    const head = el("div", "ai-opts-head");
    head.appendChild(el("span", "ai-opts-meta",
      `AI 建议 · ${r.model} · ${r.elapsed}s · 参考了 ${r.context.count} 封与收件人的往来邮件`));
    if (r.options.some((o) => o.subject)) {
      // 主题是「可选补充」：用户自己写了主题时默认不覆盖，勾上才用 AI 的
      const sw = el("label", "ai-opts-switch");
      sw.title = "勾上后，点「用这份」会连主题一起换成 AI 建议的";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.id = "aiUseSubject";
      cb.checked = !$("#cSubject").value.trim();
      sw.appendChild(cb);
      sw.appendChild(el("span", null, "同时用 AI 主题"));
      head.appendChild(sw);
    }
    box.appendChild(head);

    (r.options || []).forEach((o, i) => {
      const card = el("div", "ai-opt");
      card.appendChild(el("div", "ai-opt-title", o.title || `方案 ${i + 1}`));

      const subRow = el("div", "ai-opt-subject");
      if (o.subject) {
        subRow.appendChild(el("span", "ai-opt-subject-label", "主题"));
        subRow.appendChild(el("span", "ai-opt-subject-text", o.subject));
      } else {
        subRow.classList.add("is-empty");
        subRow.appendChild(el("span", "ai-opt-subject-label", "主题"));
        subRow.appendChild(el("span", "ai-opt-subject-text", "（本次没给主题建议）"));
      }
      card.appendChild(subRow);

      card.appendChild(el("div", "ai-opt-body", o.body || ""));

      const acts = el("div", "ai-opt-actions");
      const use = el("button", "btn btn-primary btn-sm", "用这份");
      use.type = "button";
      use.onclick = () => {
        const cb = $("#aiUseSubject");
        const next = applyAiOption(composeFields(), o, !!(cb && cb.checked));
        $("#cBody").value = next.body;
        if (next.subjectUsed) $("#cSubject").value = next.subject;
        setAiPanel(false);
        markComposeDirty();
        toast(
          next.subjectUsed
            ? `已套用「${o.title || "方案 " + (i + 1)}」，主题也一起换了`
            : `已套用「${o.title || "方案 " + (i + 1)}」`,
          2600
        );
      };
      acts.appendChild(use);

      if (o.subject) {
        const onlySub = el("button", "btn btn-sm", "只用主题");
        onlySub.type = "button";
        onlySub.title = "只把主题填进去，正文不动";
        onlySub.onclick = () => {
          $("#cSubject").value = o.subject;
          markComposeDirty();
          toast("主题已填入", 1800);
        };
        acts.appendChild(onlySub);
      }
      card.appendChild(acts);
      box.appendChild(card);
    });

    const fold = el("button", "btn btn-sm", "收起");
    fold.type = "button";
    fold.title = "收起后不会丢，点「✨ AI 建议」可以再展开";
    fold.onclick = () => setAiPanel(false);
    box.appendChild(fold);

    setAiPanel(true);   // 生成完直接展开
  }

  async function aiGenerate() {
    const btn = $("#aiBtn");
    const f = composeFields();
    if (!composeHasContent(f)) {
      setComposeStatus("先填收件人或写点内容，AI 才有东西可参考");
      return;
    }
    btn.disabled = true;
    const label = btn.textContent;
    btn.textContent = "生成中…";
    setComposeStatus("正在读取往来邮件并调用 AI，可能要十几秒…");
    try {
      const cfg = await api("/api/config/ai");
      if (!cfg.config.usable) {
        setComposeStatus("AI 还没配好，请在设置里填接口地址 / 密钥 / 模型");
        openSettings("需要先配置 AI 接口才能生成正文");
        return;
      }
      const r = await jsonPost("/api/compose/ai", {
        to: f.to, cc: f.cc, subject: f.subject, body: f.body,
      });
      renderAIOptions(r);
      setComposeStatus("生成完成，挑一份合适的即可（收起后点「✨ AI 建议」能再展开）", true);
    } catch (err) {
      setComposeStatus("AI 生成失败：" + err.message);
    } finally {
      btn.disabled = false;
      btn.textContent = label;
    }
  }

  /* ------------------------------ 邮箱账号（多邮箱） ------------------------------ */

  /** 拉一次账号列表；顶栏那个小字也在这里更新。 */
  async function loadAccounts() {
    try {
      const r = await api("/api/accounts");
      state.accounts = r.accounts || [];
      state.activeAccount = r.active || "";
      state.accountsFile = r.file || "";
      paintAccountLabel();
      // 目录的「系统目录不给删按钮」是按账号 aliases 算的，账号回来后要重画一遍
      if (state.folders.length) renderFolders();
    } catch (e) {
      state.accounts = [];
    }
    return state.accounts;
  }

  function activeAccountInfo() {
    return state.accounts.find((a) => a.name === state.activeAccount) || null;
  }

  function paintAccountLabel() {
    const a = activeAccountInfo() || state.accounts[0];
    $("#account").textContent = a ? (a.email || a.name) : "未配置邮箱";
  }

  /** 顶栏下拉：所有账号 + 一行「管理邮箱账号」。 */
  function buildAccountMenu() {
    const menu = $("#accountMenu");
    menu.innerHTML = "";
    if (!state.accounts.length) {
      menu.appendChild(el("div", "account-opt-sub", "还没有可用的邮箱账号"));
    }
    state.accounts.forEach((a) => {
      const b = el("button", "account-opt" + (a.name === state.activeAccount ? " is-active" : ""));
      b.type = "button";
      const box = el("div", "account-opt-main");
      box.appendChild(el("div", "account-opt-name", a.email || a.name));
      const tags = [a.name];
      if (a.default) tags.push("默认");
      if (!a.has_password) tags.push("未设密码");
      box.appendChild(el("div", "account-opt-sub", tags.join(" · ")));
      b.appendChild(box);
      b.appendChild(el("span", "account-check", "✓"));
      b.addEventListener("click", () => { menu.classList.remove("open"); switchAccount(a.name); });
      menu.appendChild(b);
    });
    const foot = el("div", "account-menu-foot");
    const manage = el("button", "account-manage", "⚙ 管理邮箱账号…");
    manage.type = "button";
    manage.addEventListener("click", () => { menu.classList.remove("open"); openAccounts(); });
    foot.appendChild(manage);
    menu.appendChild(foot);
  }

  /** 切换账号 = 换一整套后端状态（索引库 / 缓存 / 目录），最稳的是整页重载。 */
  async function switchAccount(name) {
    if (name === state.activeAccount) return;
    try {
      const r = await api(`/api/accounts/${encodeURIComponent(name)}/activate`, { method: "POST" });
      try { sessionStorage.setItem("mail.flash", `已切换到 ${r.email || name}`); } catch (e) {}
      location.reload();
    } catch (e) {
      toast("切换账号失败：" + e.message, 5000);
    }
  }

  /* ---- 邮箱管理弹窗 ---- */

  async function openAccounts() {
    $("#accountsModal").hidden = false;
    $("#accountStatus").textContent = "";
    $("#accountsList").innerHTML = '<div class="empty">加载中…</div>';
    await loadAccounts();
    paintAccountsList();
    buildAccountMenu();
    fillAccountForm(activeAccountInfo() || state.accounts[0] || null);
  }

  function closeAccounts() { $("#accountsModal").hidden = true; }

  function paintAccountsList() {
    const box = $("#accountsList");
    box.innerHTML = "";
    if (!state.accounts.length) {
      box.innerHTML = '<div class="empty">还没有配置邮箱账号</div>';
      return;
    }
    state.accounts.forEach((a) => {
      const row = el("div", "acc-item" + (a.name === state.activeAccount ? " is-active" : ""));
      const main = el("div", "acc-item-main");
      main.appendChild(el("div", "acc-item-name", a.email || a.name));
      const tags = [a.name];
      if (a.default) tags.push("默认");
      if (!a.has_password) tags.push("未设密码");
      main.appendChild(el("div", "acc-item-sub", tags.join(" · ")));
      row.appendChild(main);
      const del = el("button", "acc-item-del", "×");
      del.type = "button";
      del.title = "删除这个账号";
      del.addEventListener("click", (e) => { e.stopPropagation(); removeAccount(a); });
      row.appendChild(del);
      row.addEventListener("click", () => fillAccountForm(a));
      box.appendChild(row);
    });
  }

  /** 把一份账号摘要填进右侧表单；传 null = 清空成「新增」。 */
  function fillAccountForm(a) {
    state.accountEdit = a ? a.name : "";
    const imap = (a && a.imap) || {};
    const smtp = (a && a.smtp) || {};
    const al = (a && a.aliases) || {};
    $("#accName").value = a ? a.name : "";
    $("#accName").disabled = !!a;                 // 名字是主键，改名等于新建一个
    $("#accEmail").value = (a && a.email) || "";
    $("#accDisplay").value = (a && a.display_name) || "";
    $("#accUser").value = (a && a.username) || "";
    $("#accImapHost").value = imap.host || "";
    $("#accImapPort").value = imap.port || 993;
    $("#accSmtpHost").value = smtp.host || "";
    $("#accSmtpPort").value = smtp.port || 465;
    $("#accImapSsl").checked = a ? !!imap.ssl : true;
    $("#accSmtpSsl").checked = a ? !!smtp.ssl : true;
    $("#accDefault").checked = !!(a && a.default);
    $("#accAliasInbox").value = al.inbox || "";
    $("#accAliasSent").value = al.sent || "";
    $("#accAliasDrafts").value = al.drafts || "";
    $("#accAliasTrash").value = al.trash || "";
    $("#accPassword").value = "";
    $("#accPassword").placeholder = (a && a.has_password)
      ? "已保存（留空 = 不修改）"
      : "留空 = 不修改 / 未设置";
    $("#accSaveBtn").textContent = a ? "保存账号" : "新增账号";
    $("#accountMeta").textContent = a
      ? `本地索引库：${a.db || "-"}`
      : "填好后点「新增账号」，会写进 config/himalaya/config.toml（已 gitignore）";
    $("#accountStatus").textContent = "";
    $("#accountStatus").classList.remove("is-ok");
  }

  function accountPayload() {
    const iAlias = (sel) => $(sel).value.trim();
    return {
      name: $("#accName").value.trim() || state.accountEdit,
      email: $("#accEmail").value.trim(),
      display_name: $("#accDisplay").value.trim(),
      username: $("#accUser").value.trim(),
      imap_host: $("#accImapHost").value.trim(),
      imap_port: parseInt($("#accImapPort").value, 10) || 993,
      imap_ssl: $("#accImapSsl").checked,
      smtp_host: $("#accSmtpHost").value.trim(),
      smtp_port: parseInt($("#accSmtpPort").value, 10) || 465,
      smtp_ssl: $("#accSmtpSsl").checked,
      password: $("#accPassword").value,
      default: $("#accDefault").checked,
      aliases: {
        inbox: iAlias("#accAliasInbox"),
        sent: iAlias("#accAliasSent"),
        drafts: iAlias("#accAliasDrafts"),
        trash: iAlias("#accAliasTrash"),
      },
    };
  }

  function validateAccount(p, st) {
    if (!p.name) { st.textContent = "账号名（英文标识）不能为空"; return false; }
    if (!p.email) { st.textContent = "邮箱地址不能为空"; return false; }
    if (!p.imap_host) { st.textContent = "IMAP 服务器不能为空"; return false; }
    return true;
  }

  /** 保存一次账号（新增或修改）。返回 true 表示落盘成功。 */
  async function saveAccount(st) {
    const p = accountPayload();
    if (!validateAccount(p, st)) return false;
    if (state.accountEdit) {
      await jsonPost(`/api/accounts/${encodeURIComponent(state.accountEdit)}`, p, { method: "PUT" });
    } else {
      await jsonPost("/api/accounts", p);
      state.accountEdit = p.name;
      $("#accName").disabled = true;
    }
    $("#accPassword").value = "";
    await loadAccounts();
    paintAccountsList();
    return true;
  }

  async function submitAccount(e) {
    e.preventDefault();
    const st = $("#accountStatus");
    const btn = $("#accSaveBtn");
    st.classList.remove("is-ok");
    btn.disabled = true;
    const label = btn.textContent;
    btn.textContent = "保存中…";
    try {
      const created = !state.accountEdit;
      if (!await saveAccount(st)) return;
      if (created) {
        // 后端把新账号切成了当前账号 —— 整个页面的世界都变了，重载最干净
        const info = state.accounts.find((x) => x.name === state.accountEdit);
        try {
          sessionStorage.setItem("mail.flash",
            `已新增并切换到 ${(info && info.email) || state.accountEdit}`);
        } catch (e) {}
        location.reload();
        return;
      }
      fillAccountForm(state.accounts.find((a) => a.name === state.accountEdit) || null);
      st.textContent = "✓ 已保存";
      st.classList.add("is-ok");
      toast("邮箱账号已保存");
      await refreshAfterAccountChange();
    } catch (err) {
      st.classList.remove("is-ok");
      st.textContent = "保存失败：" + err.message;
    } finally {
      btn.disabled = false;
      btn.textContent = label;
    }
  }

  async function testAccount() {
    const btn = $("#accTestBtn");
    const st = $("#accountStatus");
    const p = accountPayload();
    if (!p.imap_host) { st.textContent = "先填 IMAP 服务器"; return; }
    btn.disabled = true;
    const label = btn.textContent;
    btn.textContent = "测试中…";
    st.classList.remove("is-ok");
    try {
      // 不落盘、不切账号：纯粹验这组凭据能不能连上
      const r = await jsonPost("/api/accounts/test", p);
      const samples = (r.samples || []).slice(0, 4).join(" / ");
      st.textContent = `✓ 连接正常：${r.folders} 个目录（${r.elapsed}s）${samples ? " · " + samples : ""}`;
      st.classList.add("is-ok");
    } catch (err) {
      st.classList.remove("is-ok");
      st.textContent = "测试失败：" + err.message;
    } finally {
      btn.disabled = false;
      btn.textContent = label;
    }
  }

  async function removeAccount(a) {
    if (!confirm(`删除邮箱账号「${a.name}」？\n\n账号配置和它的密码文件会被删除，服务器上的邮件不受影响。`)) return;
    const purge = confirm(
      `还要不要连本地索引库一起删掉？\n\n确定 = 一并删除（索引里的邮件列表会丢，重新添加后要重新同步）\n取消 = 保留本地索引库`
    );
    const wasActive = a.name === state.activeAccount;
    try {
      const r = await api(
        `/api/accounts/${encodeURIComponent(a.name)}?purge=${purge ? "true" : "false"}`,
        { method: "DELETE" }
      );
      if (wasActive) {
        // 删掉的是当前账号 —— 后端会自动落到另一个账号上，重载免得前后端不一致
        try { sessionStorage.setItem("mail.flash", `已删除 ${a.name}，当前账号切到 ${r.active}`); } catch (e) {}
        location.reload();
        return;
      }
      toast(`已删除账号 ${a.name}` + (r.removed_db ? "（含本地索引）" : ""), 4000);
      state.accountEdit = "";
      await refreshAfterAccountChange();
      paintAccountsList();
      fillAccountForm(activeAccountInfo() || state.accounts[0] || null);
    } catch (e) {
      toast("删除账号失败：" + e.message, 5000);
    }
  }

  /** 账号被改了之后，当前账号的世界可能变了 —— 先把账号信息拉回来，再刷侧栏 / 列表 / 状态。 */
  async function refreshAfterAccountChange() {
    await loadStatus();                    // 顺带更新 state.accounts / activeAccount
    buildAccountMenu();
    await loadFolders();
    await loadCurrent(false);
  }

  /* ------------------------------ 设置（AI 接口 + 同步） ------------------------------ */

  /** 自动同步状态 → 一行说明（设置面板里显示下一轮倒计时）。 */
  function syncStatusText(st) {
    const c = (st && st.config) || {};
    if (!c.auto_enabled) return "后台自动同步：已关闭";
    const next = st && st.next_run_in;
    const mm = next == null ? "" : `，下一轮约 ${next < 60 ? `${next} 秒` : `${Math.round(next / 60)} 分钟`}后`;
    return `后台自动同步：每 ${c.interval_minutes} 分钟一次（近 ${c.auto_since_days} 天）${mm}`;
  }

  async function openSettings(hint) {
    let failure = "";
    try {
      const [ai, sy] = await Promise.all([api("/api/config/ai"), api("/api/config/sync")]);
      const c = ai.config;
      $("#cfgEnabled").checked = !!c.enabled;
      $("#cfgBaseUrl").value = c.base_url || "";
      $("#cfgModel").value = c.model || "";
      $("#cfgApiKey").value = "";
      $("#cfgApiKey").placeholder = c.has_key ? `已保存 ${c.key_hint}（留空不修改）` : "粘贴你的 API Key";
      $("#cfgTemp").value = c.temperature;
      $("#cfgTimeout").value = c.timeout;
      $("#cfgRecent").value = c.recent_count;
      $("#cfgCtxChars").value = c.context_chars;
      $("#settingsMeta").textContent =
        `配置文件：${ai.file}` +
        (c.source === "env" ? " · 注意：环境变量 AI_* 覆盖了这里的设置" : "") +
        (c.usable ? "" : " · 当前不可用");

      const s = sy.config || {};
      $("#cfgSyncAuto").checked = !!s.auto_enabled;
      $("#cfgSyncStartup").checked = !!s.startup_enabled;
      $("#cfgSyncBody").checked = !!s.with_body;
      $("#cfgSyncInterval").value = s.interval_minutes;
      $("#cfgSyncAutoDays").value = s.auto_since_days;
      $("#cfgSyncStartupDays").value = s.startup_since_days;
      $("#cfgSyncBodyLimit").value = s.body_limit || 0;
      $("#settingsSyncMeta").textContent =
        syncStatusText(sy.status) +
        (s.source === "env" ? " · 注意：环境变量 SYNC_* 覆盖了这里的设置" : "") +
        (sy.status && sy.status.errors && sy.status.errors.length
          ? ` · 最近失败：${sy.status.errors[sy.status.errors.length - 1]}`
          : "");
    } catch (err) {
      failure = "配置读取失败：" + err.message;
    }

    // 时区这一项单独取：静态文件是随改随生效的，后端要重启才换代码，
    // 所以「新前端 + 旧后端」是开发中很常见的中间状态 —— 那时这个端点会 404，
    // 不该把整个设置面板一起拖垮（AI / 同步两块都还是好的）。
    try {
      const dp = await api("/api/config/display");
      const d = (dp && dp.config) || {};
      $("#cfgTzOffset").value = d.utc_offset || "";
      $("#settingsDisplayMeta").textContent =
        "引用头 Sent: 当前按" +
        (d.effective ? `固定偏移 ${d.effective}` : "服务器本地时区") +
        (d.source === "env" ? " · 注意：环境变量 DISPLAY_UTC_OFFSET 覆盖了这里的设置" : "");
    } catch (err) {
      $("#cfgTzOffset").value = "";
      $("#settingsDisplayMeta").textContent =
        `读取失败（${err.message}）—— 后端可能还是旧版本，重启服务后再看`;
    }

    $("#settingsStatus").textContent = hint || failure || "";
    $("#settingsModal").hidden = false;
  }

  function closeSettings() { $("#settingsModal").hidden = true; }

  function settingsPayload() {
    const p = {
      enabled: $("#cfgEnabled").checked,
      base_url: $("#cfgBaseUrl").value.trim(),
      model: $("#cfgModel").value.trim(),
      temperature: parseFloat($("#cfgTemp").value) || 0.7,
      timeout: parseInt($("#cfgTimeout").value, 10) || 60,
      recent_count: parseInt($("#cfgRecent").value, 10) || 10,
      context_chars: parseInt($("#cfgCtxChars").value, 10) || 1200,
    };
    const key = $("#cfgApiKey").value.trim();
    if (key) p.api_key = key;      // 空 = 保留原密钥
    return p;
  }

  function syncPayload() {
    return {
      auto_enabled: $("#cfgSyncAuto").checked,
      startup_enabled: $("#cfgSyncStartup").checked,
      with_body: $("#cfgSyncBody").checked,
      interval_minutes: Math.max(1, parseInt($("#cfgSyncInterval").value, 10) || 10),
      auto_since_days: Math.max(0, parseInt($("#cfgSyncAutoDays").value, 10) || 0),
      startup_since_days: Math.max(0, parseInt($("#cfgSyncStartupDays").value, 10) || 0),
      body_limit: Math.max(0, parseInt($("#cfgSyncBodyLimit").value, 10) || 0),
    };
  }

  function displayPayload() {
    return { utc_offset: $("#cfgTzOffset").value.trim() };
  }

  async function submitSettings(e) {
    e.preventDefault();
    const st = $("#settingsStatus");
    st.classList.remove("is-ok");
    try {
      const r = await jsonPost("/api/config/ai", settingsPayload(), { method: "PUT" });
      const sy = await jsonPost("/api/config/sync", syncPayload(), { method: "PUT" });
      // 时区填错时后端会 400 并说清楚要写成什么样，这句原话要露出来，别被"保存失败"盖掉
      const dp = await jsonPost("/api/config/display", displayPayload(), { method: "PUT" });
      $("#cfgApiKey").value = "";
      $("#cfgApiKey").placeholder = r.config.has_key ? `已保存 ${r.config.key_hint}（留空不修改）` : "粘贴你的 API Key";
      $("#settingsSyncMeta").textContent = syncStatusText(sy.status);
      const d = (dp && dp.config) || {};
      $("#settingsDisplayMeta").textContent =
        "引用头 Sent: 当前按" + (d.effective ? `固定偏移 ${d.effective}` : "服务器本地时区");
      st.textContent = "✓ 已保存到 config/app.toml，立即生效";
      st.classList.add("is-ok");
      toast("设置已保存");
    } catch (err) {
      st.textContent = "保存失败：" + err.message;
    }
  }

  /** 不等下一个间隔，立刻跑一轮自动同步（时间窗用自动同步设定的天数）。 */
  async function syncNowFromSettings() {
    const btn = $("#cfgSyncNow");
    const st = $("#settingsStatus");
    btn.disabled = true;
    const label = btn.textContent;
    btn.textContent = "同步中…";
    try {
      const r = await jsonPost("/api/config/sync/now", {});
      if (!r.ok) {
        st.classList.remove("is-ok");
        st.textContent = "已有同步在进行（手动或后台），稍后再试";
      } else {
        st.classList.add("is-ok");
        st.textContent = `✓ 同步完成：新增 ${r.run.new} 封，正文 ${r.run.bodies} 封（${r.run.elapsed}s）`;
        await loadFolders();
        await loadMessages(false);
      }
      $("#settingsSyncMeta").textContent = syncStatusText(r.status);
    } catch (err) {
      st.classList.remove("is-ok");
      st.textContent = "同步失败：" + err.message;
    } finally {
      btn.disabled = false;
      btn.textContent = label;
    }
  }

  async function testSettings() {
    const btn = $("#cfgTest");
    const st = $("#settingsStatus");
    btn.disabled = true;
    const label = btn.textContent;
    btn.textContent = "测试中…";
    try {
      await jsonPost("/api/config/ai", settingsPayload(), { method: "PUT" });  // 先落盘再测
      const r = await jsonPost("/api/config/ai/test", {});
      st.textContent = `✓ 连通正常（${r.model}，${r.elapsed}s，返回「${r.reply}」）`;
      st.classList.add("is-ok");
    } catch (err) {
      st.classList.remove("is-ok");
      st.textContent = "测试失败：" + err.message;
    } finally {
      btn.disabled = false;
      btn.textContent = label;
    }
  }

  async function submitCompose(e) {
    e.preventDefault();
    const btn = $("#sendBtn");
    const status = $("#composeStatus");
    const f = composeFields();           // 顺便把输入框里没回车的地址转成 chip
    // 收件人和抄送加起来至少要有一个 —— 只发抄送（Cc）也是一封正经邮件，
    // 不该因为「To 是空的」被拦住（转发的收件人本来就留空，最容易被这里挡住）。
    if (!f.to && !f.cc) {
      setComposeStatus("收件人（或抄送）不能为空：输入姓名或邮箱后按回车，会变成一个收件人框");
      chips.to.focus();
      return;
    }
    state.compose.sending = true;        // 先立旗：发送期间不再产生新的自动保存
    clearTimeout(state.compose.timer);   // 别让待触发的自动保存插在发送中间
    // 已经上路的那次自动保存要等回来 —— 它稍后返回的新草稿 uid 会覆盖 state.compose.draft，
    // 如果不等，本次发送登记的仍是旧 uid，发完草稿箱里就多出一版（AI 生成完接着点发送最容易踩）。
    if (state.compose.saving) {
      try { await state.compose.saving; } catch (e) {}
    }
    btn.disabled = true;
    btn.textContent = "发送中…";
    setComposeStatus("");
    try {
      const fd = new FormData();
      fd.set("to", f.to);
      fd.set("cc", f.cc);
      fd.set("subject", f.subject);
      fd.set("body", f.body);
      if (state.compose.reply) {
        fd.set("reply_folder", state.compose.reply.folder);
        fd.set("reply_uid", String(state.compose.reply.uid));
      }
      if (state.compose.forward) {   // 转发：后端据此把原信作为引用块拼进正文
        fd.set("forward_folder", state.compose.forward.folder);
        fd.set("forward_uid", String(state.compose.forward.uid));
      }
      if (state.compose.draft) {   // 发完顺手把草稿箱里的那一版清掉
        fd.set("draft_folder", state.compose.draft.folder);
        fd.set("draft_uid", String(state.compose.draft.uid));
      }
      for (const file of state.compose.files) fd.append("files", file, file.name);
      const r = await api("/api/send", { method: "POST", body: fd });
      toast(
        `✓ 已发送至 ${r.accepted.join("、")}` +
        ((r.dropped && r.dropped.length) ? `（已忽略 ${r.dropped.join("、")}：不是有效邮箱地址）` : "") +
        (r.sent_uid ? `（副本已存「${folderLabel(r.sent_folder)}」）` : ""),
        4200
      );
      clearTimeout(state.compose.timer);
      state.compose.dirty = false;
      state.compose.draft = null;
      state.compose.files = [];      // 先清空，免得 closeCompose 再提示一次「附件存不进草稿」
      closeCompose();
      loadFolders();
      if (state.folder === r.sent_folder) loadCurrent(false);
    } catch (err) {
      status.textContent = "发送失败：" + err.message;
    } finally {
      state.compose.sending = false;
      btn.disabled = false;
      btn.textContent = "发送 ➤";
    }
  }

  async function deleteMessage(folder, uid) {
    if (!confirm("确定删除这封邮件？将移入服务器的回收站。")) return;
    try {
      const r = await api(`/api/messages/${uid}?folder=${encodeURIComponent(folder)}`, { method: "DELETE" });
      toast(`已移入「${r.trash ? folderLabel(r.trash) : "回收站"}」`);
      state.current = null;
      $("#reader").innerHTML =
        state.viewMode === "thread" ? '<div class="empty">选择一条会话开始阅读</div>' : '<div class="empty">选择一封邮件开始阅读</div>';
      await loadFolders();
      await loadCurrent(false);
    } catch (e) {
      toast("删除失败：" + e.message);
    }
  }

  /* ------------------------------ 同步 ------------------------------ */
  /** 手动同步按当前筛选框的时间范围拉取，把范围写在按钮提示里，省得猜。 */
  function paintSyncBtn() {
    const b = $("#syncBtn");
    if (!b) return;
    const scope = rangeLabel(state.range, state.since);
    b.title = `同步：按上方筛选框的时间范围拉取（当前 ${scope}）`;
  }

  async function doSync() {
    const btn = $("#syncBtn");
    btn.disabled = true;
    const label = btn.textContent;
    const scope = rangeLabel(state.range, state.since);
    btn.textContent = "同步中…";
    try {
      const r = await api("/api/sync", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        // 筛选框选的就是同步范围："全部时间" 换算成 1990-01-01（等价于不限）
        body: JSON.stringify({
          all_folders: true,
          since: state.since === "all" ? "1990-01-01" : state.since,
          with_body: true,
        }),
      });
      toast(`同步完成（${scope}）：新增 ${r.new} 封，正文 ${r.bodies} 封，附件 ${r.attachments} 个（${r.elapsed}s）`, 4000);
      await loadFolders();
      await loadMessages(false);
    } catch (e) {
      toast(
        e.status === 429 ? "后台自动同步正在进行中，等几秒再点一次" : `同步失败：${e.message}`,
        4000
      );
    } finally {
      btn.disabled = false;
      btn.textContent = label;
    }
  }

  /* ------------------------------ 状态栏 ------------------------------ */
  async function loadStatus() {
    try {
      const s = await api("/api/status");
      state.me = (s.account && s.account.email) || "";
      // /api/status 顺带回带账号清单，省掉一次 /api/accounts 往返
      if (Array.isArray(s.accounts) && s.accounts.length) {
        state.accounts = s.accounts;
        state.activeAccount = s.active_account || (s.account && s.account.name) || "";
      }
      paintAccountLabel();
      const st = s.stats || {};
      const c = s.categories || {};
      const th = s.threads || {};
      const as = (s.auto_sync && s.auto_sync.config) || {};
      const autoLine = as.auto_enabled
        ? `自动同步 <b>每 ${as.interval_minutes} 分钟</b>（近 ${as.auto_since_days} 天）<br>`
        : "";
      $("#stats").innerHTML =
        `本地索引 <b>${st.total || 0}</b> 封<br>` +
        `无聊邮件 <b>${c.boring || 0}</b> 封（${Math.round((c.boring_ratio || 0) * 100)}%）<br>` +
        `会话 <b>${th.threads || 0}</b> 条（${th.grouped || 0} 条多人往返）<br>` +
        autoLine +
        (st.oldest ? `最早 <b>${st.oldest.slice(0, 10)}</b><br>` : "") +
        `快捷键 <b>J/K</b> 切换 · <b>/</b> 搜索`;
    } catch (e) {
      if (!state.accounts.length) $("#account").textContent = "未连接";
    }
  }

  /* ------------------------------ 事件 ------------------------------ */
  function bind() {
    let timer = null;

    $("#search").addEventListener("input", (e) => {
      clearTimeout(timer);
      const v = e.target.value.trim();
      timer = setTimeout(() => { state.q = v; state.offset = 0; loadCurrent(false); }, 300);
    });

    $("#range").addEventListener("change", (e) => {
      applyRange(e.target.value);
      state.offset = 0;
      loadCurrent(false);
    });

    // 自定义日期界线：选完日期立刻生效（同时把筛选框切到「自定义日期…」，两者始终一致）
    $("#rangeDate").addEventListener("change", (e) => {
      applyRange("custom", e.target.value);
      state.offset = 0;
      loadCurrent(false);
    });

    $("#moreBtn").addEventListener("click", () => { state.offset += state.limit; loadCurrent(true); });
    $("#syncBtn").addEventListener("click", doSync);

    // 写邮件 / 发送
    $("#composeBtn").addEventListener("click", () => openCompose());
    $("#composeClose").addEventListener("click", closeCompose);
    $("#composeCancel").addEventListener("click", closeCompose);
    bindMaskClose("#composeModal", closeCompose);   // 只在遮罩上按下并抬起才关
    $("#composeForm").addEventListener("submit", submitCompose);

    chips.to = createChipField(
      { field: "#cToField", chips: "#cToChips", menu: "#cToMenu", input: "#cTo" }, "收件人");
    chips.cc = createChipField(
      { field: "#cCcField", chips: "#cCcChips", menu: "#cCcMenu", input: "#cCc" }, "抄送");

    // 正文里拖选多行时，mouseup 会落到遮罩上 —— 这不是「点遮罩关闭」，所以用
    // mousedown/mouseup 同源判断；否则编辑到一半的窗口会被拖没了。
    function bindMaskClose(sel, onClose) {
      const mask = $(sel);
      let downOnMask = false;
      mask.addEventListener("mousedown", (e) => { downOnMask = e.target === mask; });
      mask.addEventListener("mouseup", (e) => {
        if (downOnMask && e.target === mask) onClose();
        downOnMask = false;
      });
    }

    ["#cSubject", "#cBody"].forEach((sel) => {
      $(sel).addEventListener("input", markComposeDirty);
    });
    $("#aiBtn").addEventListener("click", aiGenerate);

    // 设置
    $("#settingsBtn").addEventListener("click", () => openSettings());
    $("#settingsClose").addEventListener("click", closeSettings);
    $("#settingsCancel").addEventListener("click", closeSettings);
    bindMaskClose("#settingsModal", closeSettings);
    $("#settingsForm").addEventListener("submit", submitSettings);
    $("#cfgTest").addEventListener("click", testSettings);
    $("#cfgSyncNow").addEventListener("click", syncNowFromSettings);

    // 邮箱账号：顶栏下拉 + 管理弹窗
    $("#accountBtn").addEventListener("click", (e) => {
      e.stopPropagation();
      const m = $("#accountMenu");
      if (!m.classList.contains("open")) buildAccountMenu();
      m.classList.toggle("open");
    });
    $("#accountsClose").addEventListener("click", closeAccounts);
    bindMaskClose("#accountsModal", closeAccounts);
    $("#accountForm").addEventListener("submit", submitAccount);
    $("#accNewBtn").addEventListener("click", () => fillAccountForm(null));
    $("#accTestBtn").addEventListener("click", testAccount);

    // 关页面/刷新时，把还在写的内容用 sendBeacon 存进草稿箱
    window.addEventListener("beforeunload", () => {
      if ($("#composeModal").hidden || !state.compose.dirty) return;
      const f = composeFields();
      if (!composeHasContent(f)) return;
      try {
        navigator.sendBeacon("/api/drafts",
          new Blob([JSON.stringify(draftPayloadForBeacon())], { type: "application/json" }));
      } catch (e) {}
    });

    $("#cFiles").addEventListener("change", (e) => addComposeFiles(e.target.files));
    $("#aiToggle").addEventListener("click", () => setAiPanel(true));

    // 直接把文件拖进写信框也能加附件（桌面端顺手）
    const form = $("#composeForm");
    let dragDepth = 0;
    form.addEventListener("dragenter", (e) => {
      if (!e.dataTransfer || ![...e.dataTransfer.types].includes("Files")) return;
      e.preventDefault();
      dragDepth++;
      form.classList.add("is-dropping");
    });
    form.addEventListener("dragover", (e) => {
      if (e.dataTransfer && [...e.dataTransfer.types].includes("Files")) e.preventDefault();
    });
    form.addEventListener("dragleave", () => {
      dragDepth = Math.max(0, dragDepth - 1);
      if (!dragDepth) form.classList.remove("is-dropping");
    });
    form.addEventListener("drop", (e) => {
      if (!e.dataTransfer || !e.dataTransfer.files.length) return;
      e.preventDefault();
      dragDepth = 0;
      form.classList.remove("is-dropping");
      addComposeFiles(e.dataTransfer.files);
    });

    // 手机上阅读区是整屏覆盖层，Esc / 手势返回都能收起来
    document.addEventListener("keydown", (e) => {
      if (e.key !== "Escape") return;
      if (document.body.classList.contains("is-reading")) setReading(false);
    });

    document.querySelectorAll("#viewSeg button").forEach((b) => {
      b.addEventListener("click", () => {
        if (state.viewMode === b.dataset.view) return;
        state.viewMode = b.dataset.view;
        document.querySelectorAll("#viewSeg button").forEach((x) => x.classList.toggle("active", x === b));
        state.current = null;
        state.offset = 0;
        setReading(false);
        $("#listTitle").textContent = folderLabel(state.folder) + (state.viewMode === "thread" ? " · 会话" : "");
        $("#reader").innerHTML = state.viewMode === "thread"
          ? '<div class="empty">选择一条会话开始阅读</div>'
          : '<div class="empty">选择一封邮件开始阅读</div>';
        loadCurrent(false);
      });
    });

    $("#category").addEventListener("change", (e) => {
      state.category = e.target.value;
      state.offset = 0;
      loadCurrent(false);
    });

    $("#unreadBtn").addEventListener("click", (e) => {
      state.unreadOnly = !state.unreadOnly;
      e.target.classList.toggle("btn-primary", state.unreadOnly);
      state.offset = 0;
      loadCurrent(false);
    });

    const menu = $("#themeMenu");
    $("#themeBtn").addEventListener("click", (e) => { e.stopPropagation(); menu.classList.toggle("open"); });
    document.addEventListener("click", (e) => {
      if (!menu.contains(e.target)) menu.classList.remove("open");
      const am = $("#accountMenu");
      if (am.classList.contains("open")
          && !am.contains(e.target) && !$("#accountBtn").contains(e.target)) {
        am.classList.remove("open");
      }
    });

    document.addEventListener("keydown", (e) => {
      const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);
      if (e.key === "Escape" && !$("#composeModal").hidden) { closeCompose(); return; }
      if (e.key === "Escape" && !$("#settingsModal").hidden) { closeSettings(); return; }
      if (e.key === "Escape" && !$("#accountsModal").hidden) { closeAccounts(); return; }
      if (e.key === "/" && !typing) { e.preventDefault(); $("#search").focus(); return; }
      if (typing) return;
      if (e.key === "j" || e.key === "ArrowDown" || e.key === "k" || e.key === "ArrowUp") {
        const rows = [...document.querySelectorAll(".msg-row")];
        if (!rows.length) return;
        e.preventDefault();
        const idx = rows.findIndex((r) => r.classList.contains("is-selected"));
        const next = e.key === "j" || e.key === "ArrowDown"
          ? Math.min(rows.length - 1, idx + 1)
          : Math.max(0, idx <= 0 ? 0 : idx - 1);
        const r = rows[next === -1 ? 0 : next];
        r.scrollIntoView({ block: "nearest" });
        openMessage(r.dataset.folder, parseInt(r.dataset.uid, 10), r);
      }
    });
  }

  /* ------------------------------ 启动 ------------------------------ */
  function init() {
    buildThemeMenu();
    let saved = "ink";
    try { saved = localStorage.getItem("mail.theme") || "ink"; } catch (e) {}
    const urlTheme = new URLSearchParams(location.search).get("theme");
    applyTheme(urlTheme && THEMES.some((t) => t.id === urlTheme) ? urlTheme : saved);

    bind();
    // 时间范围默认「近 1 月」（筛选框第一个选项），算出 since 后再发请求；
    // 手动同步用的也是这个 since，所以两边天然一致
    applyRange(state.range);
    loadStatus();
    const linkedAccount = new URLSearchParams(location.search).get("account");
    loadAccounts().then(() => {
      buildAccountMenu();
      if (linkedAccount && state.activeAccount && linkedAccount !== state.activeAccount) {
        if (state.accounts.some((account) => account.name === linkedAccount)) switchAccount(linkedAccount);
        else toast(`来源账号 ${linkedAccount} 已不可用`, 5000);
      }
    });   // 顶栏账号下拉

    // 切换账号后是整页重载，用 sessionStorage 把提示语带过来
    try {
      const flash = sessionStorage.getItem("mail.flash");
      if (flash) {
        sessionStorage.removeItem("mail.flash");
        setTimeout(() => toast(flash, 4000), 300);
      }
    } catch (e) {}

    // 支持 /?folder=INBOX&uid=123 直达某封邮件（API 里的 web_url 就指向这里）
    // 也支持 /?thread=txxx 直达某条会话
    const params = new URLSearchParams(location.search);
    const folderParam = params.get("folder");
    const uidParam = params.get("uid");
    const threadParam = params.get("thread");
    if (folderParam) state.folder = folderParam;

    if (threadParam) {
      // 会话直链：切到会话视图
      state.viewMode = "thread";
      document.querySelectorAll("#viewSeg button").forEach((b) =>
        b.classList.toggle("active", b.dataset.view === "thread")
      );
    }

    paintCachedFolders();   // 有本地快照就先画侧栏，不等网络

    // 侧栏与列表并行发出：列表不再排队等目录清单（目录清单是最慢的一步）
    const boot = Promise.all([loadFolders(), loadCurrent(false)]);
    boot.then(() => {
      if (threadParam) {
        const row = document.querySelector(`.thread-row[data-thread="${threadParam}"]`);
        openThread(threadParam, row);
      } else if (uidParam) {
        const uid = parseInt(uidParam, 10);
        if (!isNaN(uid)) {
          const row = document.querySelector(`.msg-row[data-uid="${uid}"]`);
          openMessage(state.folder, uid, row);
        }
      }
    });
  }

  document.addEventListener("DOMContentLoaded", init);
})();
