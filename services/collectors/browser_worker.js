const fs = require("fs");
const path = require("path");
const crypto = require("crypto");
const { chromium } = require("playwright");

const input = JSON.parse(process.argv[2]);
const platform = input.platform;
const keyword = input.keyword;
const limits = input.limits;

const configs = {
  shixiseng: {
    search: `https://www.shixiseng.com/interns?keyword=${encodeURIComponent(keyword)}`,
    patterns: [/\/intern\/[^/?#]+/, /\/interns\/[^/?#]+/],
  },
  boss: {
    search: `https://www.zhipin.com/web/geek/job?query=${encodeURIComponent(keyword)}`,
    patterns: [/\/job_detail\/[^/?#]+/, /\/web\/geek\/job\?[^#]*securityId=/],
  },
  xiaohongshu: {
    search: `https://www.xiaohongshu.com/search_result?keyword=${encodeURIComponent(keyword)}&source=web_search_result_notes`,
    patterns: [/\/explore\/[^/?#]+/, /\/search_result\/[^/?#]+/],
  },
};

const verificationMarkers = [
  "安全验证", "请完成验证", "滑动验证", "访问异常", "验证码",
  "当前浏览器环境存在风险", "网络环境存在风险",
  "captcha", "verify you are human",
];
const loginMarkers = ["登录后查看", "请先登录", "登录/注册", "扫码登录"];

function pageState(text) {
  const folded = String(text || "").toLowerCase();
  return {
    verification: verificationMarkers.some((item) => folded.includes(item.toLowerCase())),
    login: loginMarkers.some((item) => folded.includes(item.toLowerCase())),
  };
}

function xiaohongshuNoteUnavailable(url, title, text) {
  const combined = `${title || ""}\n${text || ""}`.toLowerCase();
  return (
    String(url || "").includes("/404") ||
    combined.includes("当前笔记暂时无法浏览") ||
    combined.includes("你访问的页面不见了") ||
    combined.includes("请打开小红书app扫码查看")
  );
}

function externalId(url) {
  const patterns = {
    boss: [/\/job_detail\/([^/?#]+)/, /[?&]securityId=([^&#]+)/],
    shixiseng: [/\/intern\/([^/?#]+)/, /\/interns\/([^/?#]+)/],
    xiaohongshu: [/\/explore\/([^/?#]+)/, /\/search_result\/([^/?#]+)/],
  };
  for (const pattern of patterns[platform]) {
    const match = url.match(pattern);
    if (match) return match[1];
  }
  return crypto.createHash("sha256").update(url).digest("hex").slice(0, 24);
}

function bossListCandidateAllowed(context) {
  const compact = String(context || "").replace(/\s+/g, "").toLowerCase();
  const excluded = (limits.excluded_companies || [])
    .map((name) => String(name).replace(/\s+/g, "").toLowerCase())
    .filter(Boolean);
  if (excluded.some((name) => compact.includes(name))) return false;
  const targets = (limits.target_companies || [])
    .map((name) => String(name).replace(/\s+/g, "").toLowerCase())
    .filter(Boolean);
  if (limits.company_filter_mode === "仅目标公司" && targets.length) {
    return targets.some((name) => compact.includes(name));
  }
  return true;
}

async function matchingLinks(page) {
  const rows = await page.locator("a[href]").evaluateAll((elements) =>
    elements.map((element) => {
      const container = element.closest(
        'li, article, section, [class*="card"], [class*="item"]'
      );
      return {
        href: element.href || "",
        title: (element.innerText || element.textContent || "").trim(),
        context: (container?.innerText || "").trim(),
      };
    })
  );
  const output = [];
  const seen = new Set();
  for (const row of rows) {
    if (!configs[platform].patterns.some((pattern) => pattern.test(row.href))) continue;
    const clean = row.href.split("#")[0];
    if (seen.has(clean)) continue;
    seen.add(clean);
    output.push({
      url: clean,
      title: row.title.replace(/\s+/g, " ").slice(0, 300),
      context: row.context.slice(0, 4000),
    });
    if (output.length >= limits.max_items_per_keyword) break;
  }
  return output;
}

async function run() {
  const result = {
    platform,
    keyword,
    items: [],
    warnings: [],
    error: "",
    login_required: false,
    verification_required: false,
    prefiltered_count: 0,
  };
  let context;
  let ownsContext = true;
  const connectedPages = [];
  try {
    fs.mkdirSync(input.profile_dir, { recursive: true });
    let connectedBrowser;
    try {
      connectedBrowser = await chromium.connectOverCDP(
        `http://127.0.0.1:${input.remote_debug_port}`,
        { timeout: 3000 }
      );
      context = connectedBrowser.contexts()[0];
      ownsContext = !context;
    } catch (_) {
      connectedBrowser = undefined;
    }
    if (!context) {
      context = await chromium.launchPersistentContext(input.profile_dir, {
        executablePath: input.chrome_path,
        headless: false,
        viewport: null,
        args: ["--no-first-run", "--no-default-browser-check", "--disable-quic"],
      });
      ownsContext = true;
    }
    context.setDefaultTimeout(12000);
    const page = ownsContext
      ? context.pages()[0] || (await context.newPage())
      : await context.newPage();
    if (!ownsContext) connectedPages.push(page);
    try {
      await page.goto(configs[platform].search, {
        waitUntil: platform === "xiaohongshu" ? "commit" : "domcontentloaded",
        timeout: platform === "xiaohongshu" ? 20000 : 30000,
      });
    } catch (error) {
      if (platform !== "xiaohongshu" || error?.name !== "TimeoutError") throw error;
      result.login_required = true;
      result.error =
        "小红书网页入口未响应。请先点击“打开小红书登录窗口”，" +
        "人工登录并完成可能出现的安全验证，保持窗口开启后再扫描；" +
        "如果登录窗口也无法打开，请稍后再试。";
      return result;
    }
    await page.waitForTimeout(platform === "xiaohongshu" ? 4000 : 2500);
    const searchText = (await page.locator("body").innerText()).slice(0, 30000);
    const state = pageState(searchText);
    if (state.verification) {
      result.verification_required = true;
      result.error = "平台触发了安全验证，扫描已停止，请在登录窗口人工完成验证。";
      return result;
    }
    if (state.login && searchText.length < 1200) {
      result.login_required = true;
      result.error = "登录状态可能已失效，请先点击“打开登录窗口”。";
      return result;
    }
    const links = await matchingLinks(page);
    if (!links.length) {
      result.warnings.push(
        "页面没有提取到岗位链接；可能没有结果、需要登录，或平台页面结构已变化。"
      );
    }
    const detailPage = await context.newPage();
    if (!ownsContext) connectedPages.push(detailPage);
    const deadline = Date.now() + limits.timeout_seconds * 1000;
    for (const row of links.slice(0, limits.max_details)) {
      if (Date.now() >= deadline) {
        result.warnings.push("已达到本次扫描时间上限。");
        break;
      }
      if (platform === "boss" && !bossListCandidateAllowed(row.context)) {
        result.prefiltered_count += 1;
        continue;
      }
      const id = externalId(row.url);
      let bodyText = row.context;
      let finalUrl = row.url;
      let title = row.title;
      let snapshotPath = "";
      try {
        await detailPage.goto(row.url, {
          waitUntil: platform === "xiaohongshu" ? "commit" : "domcontentloaded",
          timeout: 25000,
        });
        await detailPage.waitForTimeout(1000);
        finalUrl = detailPage.url();
        title = title || (await detailPage.title()).trim();
        bodyText = (await detailPage.locator("body").innerText()).slice(0, 25000);
        if (
          platform === "xiaohongshu" &&
          xiaohongshuNoteUnavailable(finalUrl, title, bodyText)
        ) {
          result.warnings.push(`笔记 ${id} 当前仅允许在 App 内查看，已跳过。`);
          continue;
        }
        const detailState = pageState(bodyText);
        if (detailState.verification) {
          result.verification_required = true;
          result.warnings.push("读取详情时触发安全验证，已停止后续详情采集。");
          break;
        }
        if (platform === "xiaohongshu") {
          fs.mkdirSync(input.snapshot_dir, { recursive: true });
          const safeId = id.replace(/[^a-zA-Z0-9_-]/g, "_").slice(0, 80);
          const absolute = path.join(input.snapshot_dir, `${safeId}.png`);
          await detailPage.screenshot({ path: absolute, fullPage: false });
          snapshotPath = path.relative(input.app_dir, absolute);
        }
      } catch (error) {
        result.warnings.push(`详情 ${id} 读取不完整：${String(error).slice(0, 120)}`);
      }
      result.items.push({
        platform,
        external_id: id,
        url: finalUrl.split("#")[0],
        access_url: finalUrl.split("#")[0],
        title: title.slice(0, 300),
        raw_text: bodyText.slice(0, 25000),
        source_keyword: keyword,
        snapshot_path: snapshotPath,
      });
    }
  } catch (error) {
    result.error = `${error.name || "Error"}: ${error.message || String(error)}`;
  } finally {
    if (ownsContext && context) {
      await context.close().catch(() => {});
    } else {
      for (const page of connectedPages) {
        await Promise.race([
          page.close().catch(() => {}),
          new Promise((resolve) => setTimeout(resolve, 1000)),
        ]);
      }
    }
  }
  return result;
}

run()
  .then((result) => {
    process.stdout.write(JSON.stringify(result), () => process.exit(0));
  })
  .catch((error) => {
    const failure = JSON.stringify({
        platform,
        keyword,
        items: [],
        warnings: [],
        error: `${error.name || "Error"}: ${error.message || String(error)}`,
        login_required: false,
        verification_required: false,
        prefiltered_count: 0,
      });
    process.stdout.write(failure, () => process.exit(1));
  });
