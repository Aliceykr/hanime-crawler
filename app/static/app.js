/* hanime-crawler 前端逻辑：首页 / 搜索 / 详情(缓存优先+任务轮询) / 错误提示 */
(function () {
  "use strict";

  var view = document.getElementById("view");
  var errorBar = document.getElementById("errorBar");
  var searchInput = document.getElementById("searchInput");
  var sortSelect = document.getElementById("sortSelect");

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function showError(msg) {
    errorBar.textContent = msg;
    errorBar.classList.remove("hidden");
  }
  function hideError() { errorBar.classList.add("hidden"); }

  function showLoading(text) {
    view.innerHTML =
      '<div class="loading"><div class="spinner"></div><div>' + esc(text) + "</div></div>";
  }

  function renderCard(c) {
    var cover = c.cover_url
      ? '<div class="card-thumb"><img loading="lazy" alt="" src="' + esc(c.cover_url) + '">'
      : '<div class="card-thumb">';
    var badge = c.duration
      ? '<span class="card-badge">' + esc(c.duration) + "</span>" : "";
    var stats = c.views
      ? '<span class="card-stats">' + esc(c.views >= 10000 ? (c.views / 10000).toFixed(1) + "萬" : String(c.views)) + "</span>"
      : "";
    var sub = [c.uploader, c.published_at].filter(Boolean).join(" · ");
    return (
      '<div class="card" data-code="' + esc(c.code) + '" role="button" tabindex="0">' +
      cover + stats + badge + "</div>" +
      '<div class="card-body"><div class="card-title">' + esc(c.title) + "</div>" +
      (sub ? '<div class="card-sub">' + esc(sub) + "</div>" : "") +
      "</div></div>"
    );
  }

  function bindCards(root) {
    Array.prototype.forEach.call(root.querySelectorAll(".card"), function (card) {
      function open() { openVideo(card.getAttribute("data-code")); }
      card.addEventListener("click", open);
      card.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); }
      });
    });
  }

  function renderGrid(cards) {
    if (!cards || !cards.length) {
      view.innerHTML = '<div class="loading">没有找到结果</div>';
      return;
    }
    var div = document.createElement("div");
    div.className = "grid";
    div.innerHTML = cards.map(renderCard).join("");
    view.innerHTML = "";
    view.appendChild(div);
    bindCards(view);
  }

  function renderHome(rows) {
    var html = "";
    Object.keys(rows || {}).forEach(function (title) {
      var cards = rows[title] || [];
      if (!cards.length) return;
      html +=
        '<section class="home-row"><h2>' + esc(title) + "</h2>" +
        '<div class="grid">' + cards.map(renderCard).join("") + "</div></section>";
    });
    view.innerHTML = html || '<div class="loading">首页为空</div>';
    bindCards(view);
  }

  function renderDetail(data) {
    var v = data.video || {};
    var cover = v.cover_url
      ? '<img alt="" src="' + esc(v.cover_url) + '">' : "";
    var tags = (v.tags || []).map(function (t) {
      return '<span class="tag">' + esc(t) + "</span>";
    }).join("");
    var sources = (v.sources || []).map(function (s) {
      return '<a class="src-btn" href="' + esc(s.url) + '" target="_blank" rel="noopener noreferrer">' +
        esc(s.resolution) + "</a>";
    }).join("");
    var related = (v.related || []).map(renderCard).join("");
    var comments = (v.comments || []).map(function (c) {
      return '<div class="comment"><div class="comment-head">' +
        '<span class="comment-author">' + esc(c.author) + "</span>" +
        (c.created_at ? '<span class="comment-time">' + esc(c.created_at) + "</span>" : "") +
        (c.likes != null ? '<span class="comment-time">👍 ' + esc(String(c.likes)) + "</span>" : "") +
        "</div><div class=\"comment-body\">" + esc(c.content) + "</div></div>";
    }).join("");

    view.innerHTML =
      '<button class="back-btn" id="backBtn" type="button">← 返回</button>' +
      '<div class="detail">' +
      '<div class="detail-cover">' + cover + "</div>" +
      '<div class="detail-info">' +
      "<h1>" + esc(v.title) + "</h1>" +
      '<div class="meta">' +
      (v.views != null ? "<span>观看 <b>" + esc(String(v.views)) + "</b></span>" : "") +
      (v.likes != null ? "<span>点赞 <b>" + esc(String(v.likes)) + "</b></span>" : "") +
      (v.published_at ? "<span>日期 <b>" + esc(v.published_at) + "</b></span>" : "") +
      (v.artist ? "<span>作者 <b>" + esc(v.artist) + "</b></span>" : "") +
      "</div>" +
      (v.title_cn ? '<div class="meta">中文标题：' + esc(v.title_cn) + "</div>" : "") +
      (tags ? '<div class="tag-list">' + tags + "</div>" : "") +
      (sources ? '<div class="sources">' + sources + "</div>" : "") +
      (v.description ? '<div class="desc">' + esc(v.description) + "</div>" : "") +
      "</div></div>" +
      (related ? '<section class="related"><h3>相关视频</h3><div class="grid">' + related + "</div></section>" : "") +
      (comments ? '<section class="comments"><h3>评论</h3>' + comments + "</section>" : "");

    document.getElementById("backBtn").addEventListener("click", function () { loadHome(); });
    bindCards(view);
    window.scrollTo(0, 0);
  }

  function renderTaskProgress(taskId) {
    view.innerHTML =
      '<div class="task-progress"><div class="spinner"></div>' +
      '<div>正在抓取详情…<br><small>任务 #' + esc(String(taskId)) + "</small></div></div>";
  }

  function openVideo(code) {
    hideError();
    showLoading("加载详情…");
    fetch("/api/video/" + encodeURIComponent(code))
      .then(function (resp) {
        if (resp.status === 202) return resp.json().then(function (body) {
          renderTaskProgress(body.task_id);
          pollTask(body.task_id, code, 0);
          throw null;
        });
        if (!resp.ok) return resp.json().then(function (b) { throw new Error(b.detail || "HTTP " + resp.status); });
        return resp.json();
      })
      .then(function (data) { renderDetail(data); })
      .catch(function (e) {
        if (e === null) return; // 已转轮询
        showError("加载失败：" + (e && e.message ? e.message : "未知错误"));
        showLoading("加载失败");
      });
  }

  function pollTask(taskId, code, tries) {
    if (tries > 120) { showError("任务超时，请稍后重试"); return; }
    fetch("/api/task/" + taskId)
      .then(function (r) { return r.json(); })
      .then(function (t) {
        if (t.status === "completed") { openVideo(code); return; }
        if (t.status === "failed") {
          showError("抓取失败：" + (t.error || "未知错误"));
          view.innerHTML = '<div class="loading">抓取失败：' + esc(t.error || "") + "</div>";
          return;
        }
        setTimeout(function () { pollTask(taskId, code, tries + 1); }, 1500);
      })
      .catch(function () { setTimeout(function () { pollTask(taskId, code, tries + 1); }, 2000); });
  }

  function doSearch() {
    hideError();
    var q = searchInput.value.trim();
    if (!q) { showError("请输入搜索关键词"); return; }
    showLoading("搜索…");
    var url = "/api/search?query=" + encodeURIComponent(q) + "&page=1";
    if (sortSelect.value) url += "&sort=" + encodeURIComponent(sortSelect.value);
    fetch(url)
      .then(function (r) {
        if (!r.ok) return r.json().then(function (b) { throw new Error(b.detail || "HTTP " + r.status); });
        return r.json();
      })
      .then(function (data) {
        var head = document.createElement("div");
        head.style.cssText = "margin:4px 0 14px;color:#9a9aa3;font-size:13px;";
        head.textContent = "“" + q + "” 共 " + data.cards.length + " 条 · 共 " + data.total_pages + " 页";
        renderGrid(data.cards);
        view.insertBefore(head, view.firstChild);
      })
      .catch(function (e) {
        showError("搜索失败：" + (e && e.message ? e.message : "未知错误"));
        showLoading("搜索失败");
      });
  }

  function loadHome() {
    hideError();
    showLoading("加载首页…");
    fetch("/api/home")
      .then(function (r) {
        if (!r.ok) return r.json().then(function (b) { throw new Error(b.detail || "HTTP " + r.status); });
        return r.json();
      })
      .then(function (data) {
        if (data.structure_warning) {
          showError("⚠ " + data.structure_warning);
        }
        renderHome(data.rows);
      })
      .catch(function (e) {
        showError("首页加载失败（请检查代理 127.0.0.1:7890 是否可用）：" +
          (e && e.message ? e.message : ""));
        showLoading("首页加载失败");
      });
  }

  function loadPreview() {
    hideError();
    var d = new Date();
    var month = String(d.getMonth() + 1).padStart(2, "0");
    var date = String(d.getFullYear()) + month;
    showLoading("加载新番预告…");
    fetch("/api/previews/" + date)
      .then(function (r) {
        if (!r.ok) return r.json().then(function (b) { throw new Error(b.detail || "HTTP " + r.status); });
        return r.json();
      })
      .then(function (data) {
        var head = document.createElement("div");
        head.style.cssText = "margin:4px 0 14px;color:#9a9aa3;font-size:13px;";
        head.textContent = "新番預告 · " + date + " · " + data.items.length + " 部";
        renderGrid(data.items);
        view.insertBefore(head, view.firstChild);
      })
      .catch(function (e) {
        showError("预告加载失败（本月可能尚未发布或站点无数据）：" +
          (e && e.message ? e.message : ""));
        showLoading("预告加载失败");
      });
  }

  document.getElementById("searchBtn").addEventListener("click", doSearch);
  document.getElementById("previewBtn").addEventListener("click", loadPreview);
  searchInput.addEventListener("keydown", function (e) { if (e.key === "Enter") doSearch(); });
  document.getElementById("brandBtn").addEventListener("click", loadHome);

  loadHome();
})();
