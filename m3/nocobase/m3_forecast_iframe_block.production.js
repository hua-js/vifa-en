// NocoBase JS 区块：生产环境。固定域名，禁止拼接用户输入。
const M3_IFRAME_URL = "https://opdash.lvkpower.com/ett";

void (async () => {
  "use strict";

  const cleanupKey = Symbol.for("vifa.m3.forecastIframe.cleanup");
  const previousCleanup = window[cleanupKey];
  if (typeof previousCleanup === "function") previousCleanup();

  let currentToken = null;
  let messageHandler = null;

  const fail = () => {
    currentToken = null;
    const message = document.createElement("p");
    message.textContent = "预测看板暂不可用";
    message.setAttribute("role", "alert");
    ctx.element.replaceChildren(message);
  };

  try {
    const iframeUrl = new URL(M3_IFRAME_URL);
    if (!['http:', 'https:'].includes(iframeUrl.protocol)) throw new TypeError("invalid iframe protocol");
    if (iframeUrl.username || iframeUrl.password || iframeUrl.pathname !== "/ett" || iframeUrl.search || iframeUrl.hash) {
      throw new TypeError("invalid iframe URL");
    }
    const iframeOrigin = iframeUrl.origin;
    currentToken = await ctx.getVar("ctx.token");
    if (typeof currentToken !== "string" || !/^[\x21-\x7e]{1,4096}$/.test(currentToken)) throw new TypeError("invalid current-user token");

    const iframe = document.createElement("iframe");
    iframe.src = iframeUrl.href;
    iframe.title = "场站未来能耗预测";
    iframe.setAttribute("sandbox", "allow-scripts allow-same-origin");
    iframe.referrerPolicy = "no-referrer";
    iframe.style.width = "100%";
    iframe.style.height = "calc(100vh - 120px)";
    iframe.style.minHeight = "720px";
    iframe.style.border = "0";
    iframe.style.display = "block";

    messageHandler = (event) => {
      if (event.source !== iframe.contentWindow || event.origin !== iframeOrigin) return;
      const message = event.data;
      if (message === null || typeof message !== "object" || Array.isArray(message)) return;
      const prototype = Object.getPrototypeOf(message);
      if (prototype !== Object.prototype && prototype !== null) return;
      const keys = Object.keys(message).sort();
      if (keys.length !== 2 || keys[0] !== "nonce" || keys[1] !== "type") return;
      if (message.type !== "vifa-m3-auth-ready" || !/^[a-f0-9]{32}$/.test(message.nonce)) return;
      if (currentToken === null || iframe.contentWindow === null) return;
      iframe.contentWindow.postMessage({
        type: "vifa-m3-auth-token",
        nonce: message.nonce,
        token: currentToken,
      }, iframeOrigin);
    };

    window.addEventListener("message", messageHandler);
    ctx.element.replaceChildren(iframe);
    window[cleanupKey] = () => {
      currentToken = null;
      if (messageHandler !== null) window.removeEventListener("message", messageHandler);
      if (iframe.isConnected) iframe.remove();
      messageHandler = null;
      window[cleanupKey] = null;
    };
  } catch (_error) {
    if (messageHandler !== null) window.removeEventListener("message", messageHandler);
    fail();
  }
})();
