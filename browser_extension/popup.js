const captureButton = document.getElementById("capture");
const statusElement = document.getElementById("status");
const summaryElement = document.getElementById("pageSummary");
const tokenInput = document.getElementById("token");
const pairingPanel = document.getElementById("pairingPanel");

let activeTab;

function setStatus(message, state = "") {
  statusElement.textContent = message;
  statusElement.dataset.state = state;
}

function extractVisibleJobPage() {
  const clean = (value) => String(value || "").replace(/\s+/g, " ").trim();
  const firstText = (selectors) => {
    for (const selector of selectors) {
      const element = document.querySelector(selector);
      const text = clean(element?.innerText || element?.textContent);
      if (text) return text;
    }
    return "";
  };
  const longestBlock = (selectors) => {
    const values = selectors
      .flatMap((selector) => Array.from(document.querySelectorAll(selector)))
      .map((element) => ({
        element,
        text: String(element.innerText || element.textContent || "").trim(),
      }))
      .filter((entry) => entry.text.length >= 80)
      .sort((left, right) => right.text.length - left.text.length);
    return values[0] || {
      element: document.body,
      text: String(document.body?.innerText || "").trim(),
    };
  };
  const hostname = location.hostname.toLowerCase();
  const isBoss = hostname === "zhipin.com" || hostname.endsWith(".zhipin.com");
  const isXhs = hostname === "xiaohongshu.com" || hostname.endsWith(".xiaohongshu.com");
  const isShixiseng = hostname === "shixiseng.com" || hostname.endsWith(".shixiseng.com");
  if (!isBoss && !isXhs && !isShixiseng) {
    throw new Error("请在小红书、BOSS 直聘或实习僧岗位页面使用扩展。");
  }
  const selectors = isBoss
    ? [".job-sec-text", ".job-detail", "[class*='job-detail']", "main"]
    : isShixiseng
      ? [".job_part", ".job-detail", "[class*='job-content']", "[class*='job-detail']", "main"]
      : [".note-content", "[class*='note-content']", "[class*='detail-desc']", "main"];
  const content = longestBlock(selectors);
  const dateSelectors = isBoss
    ? [".job-time", "[class*='update-time']", "[class*='publish-time']"]
    : isShixiseng
      ? [".job_date", "[class*='release-time']", "[class*='publish-time']"]
      : [".date", "[class*='publish-time']", "[class*='bottom-container']"];
  const visibleDateText = firstText(dateSelectors);
  const dateSource = `${visibleDateText}\n${content.text}`;
  const dateMatch = dateSource.match(
    /(?:20\d{2}\s*[年./-]\s*\d{1,2}\s*[月./-]\s*\d{1,2}日?|(?<!\d)\d{1,2}\s*(?:月|[./-])\s*\d{1,2}日?(?!\d)|今天|昨天|前天|\d{1,3}天前|\d{1,2}小时前)/
  );
  const carouselImages = isXhs
    ? document.querySelectorAll("[class*='swiper'] img, [class*='carousel'] img, [class*='slider'] img").length
    : 0;
  const imageCount = Math.max(
    content.element?.querySelectorAll?.("img").length || 0,
    carouselImages,
  );
  const titleSelectors = isBoss
    ? [".job-name", "h1"]
    : isShixiseng
      ? [".new_job_name", ".job-name", "h1"]
      : [".title", "h1"];
  const firstContentLine = String(content.text || "")
    .split(/\r?\n/)
    .map(clean)
    .find((line) => line.length >= 4 && line.length <= 100) || "";
  // Xiaohongshu keeps recommendation cards behind the open note. A global
  // `.title` selector can therefore return a different post; the visible
  // note text is the safer source.
  const pageTitle = isXhs
    ? firstContentLine || firstText(titleSelectors) || document.title
    : firstText(titleSelectors) || document.title;
  return {
    url: location.href,
    title: pageTitle,
    company: firstText(
      isBoss
        ? [".company-name", "[class*='company-name']"]
        : isShixiseng
          ? [".com-name", ".company-name", "[class*='company-name']"]
          : []
    ),
    role: isXhs ? "" : firstText(titleSelectors),
    location: firstText(
      isBoss
        ? [".job-address", "[class*='job-address']"]
        : isShixiseng
          ? [".job_position", ".job-address", "[class*='job-address']"]
          : []
    ),
    salary: firstText(
      isBoss
        ? [".salary", "[class*='salary']"]
        : isShixiseng
          ? [".job_money", ".salary", "[class*='salary']"]
          : []
    ),
    author: firstText(isXhs ? [".username", "[class*='author'] [class*='name']"] : []),
    posted_at: dateMatch ? dateMatch[0] : "",
    raw_text: content.text.slice(0, 50000),
    image_count: imageCount,
  };
}

async function initialize() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  activeTab = tab;
  const hostname = new URL(tab.url || "https://invalid.local").hostname;
  const supported = ["xiaohongshu.com", "zhipin.com", "shixiseng.com"]
    .some((domain) => hostname === domain || hostname.endsWith(`.${domain}`));
  const platformLabel = hostname.includes("xiaohongshu")
    ? "小红书"
    : hostname.includes("shixiseng")
      ? "实习僧"
      : "BOSS 直聘";
  summaryElement.textContent = supported
    ? `已识别：${platformLabel}`
    : "请先打开小红书、BOSS 或实习僧岗位页面。";
  captureButton.disabled = !supported;
  const stored = await chrome.storage.local.get("careerOsToken");
  tokenInput.value = stored.careerOsToken || "";
  pairingPanel.open = !stored.careerOsToken;
}

document.getElementById("saveToken").addEventListener("click", async () => {
  const token = tokenInput.value.trim();
  if (!token) {
    setStatus("请填写求职助手显示的配对码。", "error");
    return;
  }
  await chrome.storage.local.set({ careerOsToken: token });
  pairingPanel.open = false;
  setStatus("配对码已保存在当前浏览器。", "success");
});

captureButton.addEventListener("click", async () => {
  captureButton.disabled = true;
  setStatus("正在读取当前页面……");
  try {
    const stored = await chrome.storage.local.get("careerOsToken");
    const token = stored.careerOsToken || tokenInput.value.trim();
    if (!token) throw new Error("请先填写并保存配对码。");
    const [{ result }] = await chrome.scripting.executeScript({
      target: { tabId: activeTab.id },
      func: extractVisibleJobPage,
    });
    const screenshot = await chrome.tabs.captureVisibleTab(activeTab.windowId, {
      format: "jpeg",
      quality: 72,
    });
    const response = await fetch("http://127.0.0.1:8765/capture", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Career-OS-Token": token,
      },
      body: JSON.stringify({
        ...result,
        manual_decision: document.getElementById("decision").value,
        screenshot_data_url: screenshot,
      }),
    });
    const receipt = await response.json();
    if (!response.ok || !receipt.ok) throw new Error(receipt.error || "收录失败。");
    const quality = receipt.availability_status === "expired"
      ? "岗位可能已失效，已入账但不评分"
      : receipt.capture_kind === "image"
        ? "图片 JD 已保存截图，请在助手中补全文字"
        : receipt.content_level === "full"
      ? `完整 JD，完整度 ${receipt.completeness_score}%，请在助手中逐条判断`
      : `已入账，完整度 ${receipt.completeness_score}%，待补全后评分`;
    setStatus(`#${String(receipt.item_id).padStart(4, "0")} ${quality}`, "success");
  } catch (error) {
    setStatus(error.message || String(error), "error");
  } finally {
    captureButton.disabled = false;
  }
});

document.getElementById("openAssistant").addEventListener("click", () => {
  chrome.tabs.create({ url: "http://localhost:8501" });
});

initialize().catch((error) => setStatus(error.message || String(error), "error"));
