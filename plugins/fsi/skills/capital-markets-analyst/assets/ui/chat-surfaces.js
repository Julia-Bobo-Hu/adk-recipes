// Preview of the two chat surfaces (see chat-surfaces.html).
//
// The page is laid out as a chat: an example request, then the surface a chat client would
// show for it.
//
// Part 1 plays the host side of the MCP Apps protocol for assets/mcp-app/ripple.html:
// it loads the view document into a sandboxed frame under the default content security
// policy, answers ui/initialize, delivers the tool input and result, and forwards the
// view's tools/call requests to the workstation, which returns what the MCP server returns.
// As in a chat, the request above the view does not change when the view calls the tool
// itself; a line under the view counts those calls and shows the last one.
// Part 2 draws the A2UI card messages with a small renderer for the components they use.
// The card is built for the book and scenario the view shows, so the page tells one story.
// Neither part is a real client; both exist so the surfaces can be checked without one.
// The page's "Developer view" section shows what goes over the wire.
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const HOST_INFO = { name: "workstation-preview", version: "1.1.0" };
  // MCP Apps default policy for a view that declares no extra origins. In a sandboxed
  // srcdoc frame there is no "self", so only the inline allowances remain.
  const DEFAULT_CSP =
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; media-src data:; connect-src 'none'";
  // A host caps the height of a view shown inline in the conversation.
  const INLINE_MAX_HEIGHT = 800;
  const SHOW_TOOL = "show_ripple_graph";
  const DEFAULT_BOOK = "next27_flagship";
  const DEFAULT_TICKER = "UAL";

  // Switches for checking how the view copes with a less capable host, and for opening the
  // developer section from a link:
  //   ?tools=0  the host does not let the view call server tools (the view becomes read-only);
  //   ?strip=1  the host passes the tool result without its structured content;
  //   ?dev=1    the developer section starts open.
  const query = new URLSearchParams(window.location.search);
  const OFFER_TOOLS = query.get("tools") !== "0";
  const STRIP_STRUCTURED = query.get("strip") === "1";

  const state = {
    theme: "light",
    books: [],
    viewReady: false,
    viewShows: null, // book and scenario of the result the view shows now
    viewCalls: 0, // tools/call requests from the view since the model's call
    cardTicker: {}, // per book, the holding last shown as a card
    cardSeq: 0, // the newest card request; an older reply is dropped
  };

  async function getJson(path) {
    const response = await fetch(path, { cache: "no-store" });
    if (!response.ok) throw new Error(`${path} returned HTTP ${response.status}`);
    return response.json();
  }
  async function postJson(path, body) {
    const response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `${path} returned HTTP ${response.status}`);
    return payload;
  }
  function showPageError(message) {
    const box = $("page-error");
    box.textContent = message;
    box.hidden = false;
  }

  // "+50 bp", "-20%", "unchanged". The tool returns decimal strings; they are shown as
  // text and never go through a float.
  function moveText(value, unit) {
    const text = String(value ?? "").trim();
    const match = /^([+-]?)(\d+(?:\.\d+)?)$/.exec(text);
    if (!match) return `${text}${unit}`;
    if (/^0+(?:\.0+)?$/.test(match[2])) return "unchanged";
    return `${match[1] === "-" ? "-" : "+"}${match[2]}${unit}`;
  }
  const scenarioText = (rate, oil) => `rates ${moveText(rate, " bp")}, oil ${moveText(oil, "%")}`;
  // Tool arguments as one line: "book_id next27_flagship · rate_bps 50 · oil_pct 25".
  const argsText = (args) =>
    Object.entries(args || {})
      .map(([key, value]) => `${key} ${value}`)
      .join(" · ");

  // ------------------------------------------------------------------ MCP App host
  const frame = $("app-frame");

  function logMessage(direction, message) {
    const list = $("rpc-log");
    let label = message.method || (message.error ? `error ${message.error.code}` : "result");
    if (message.method === "tools/call" && message.params) {
      label += ` ${message.params.name} ${JSON.stringify(message.params.arguments || {})}`;
    } else if (message.method === "ui/notifications/size-changed" && message.params) {
      label += ` ${message.params.width}x${message.params.height}`;
    } else if (message.method === "ui/notifications/tool-input" && message.params) {
      label += ` ${JSON.stringify(message.params.arguments || {})}`;
    } else if (message.method === undefined && message.id !== undefined) {
      label += ` (id ${message.id})`;
    }
    const item = document.createElement("li");
    item.className = direction === "to view" ? "to" : "from";
    const dir = document.createElement("span");
    dir.className = "dir";
    dir.textContent = direction === "to view" ? "host → view" : "view → host";
    item.append(dir, label);
    list.append(item);
    while (list.children.length > 80) list.firstElementChild.remove();
    list.scrollTop = list.scrollHeight;
  }

  function sendToView(message) {
    logMessage("to view", message);
    frame.contentWindow.postMessage(message, "*");
  }

  const hostContext = () => ({
    theme: state.theme,
    displayMode: "inline",
    availableDisplayModes: ["inline"],
    platform: "web",
    containerDimensions: { maxHeight: INLINE_MAX_HEIGHT },
  });

  function modelArguments() {
    return { book_id: $("mcp-book").value || DEFAULT_BOOK, rate_bps: $("mcp-rate").value, oil_pct: $("mcp-oil").value };
  }

  // The view shows a new result: both developer forms take its book and scenario, and the
  // card is rebuilt for it. Returns the result's structured content, or null for an error result.
  function followView(result) {
    const view = result && !result.isError ? result.structuredContent : null;
    if (!view || !view.scenario) return null;
    if (Array.isArray(view.books)) setBooks(view.books);
    const { rate_bps: rate, oil_pct: oil } = view.scenario;
    state.viewShows = { book_id: view.book_id, rate_bps: String(rate), oil_pct: String(oil) };
    $("mcp-book").value = view.book_id;
    $("mcp-rate").value = rate;
    $("mcp-oil").value = oil;
    if ($("a2ui-book").value !== view.book_id) {
      $("a2ui-book").value = view.book_id;
      fillTickers();
    }
    $("a2ui-rate").value = rate;
    $("a2ui-oil").value = oil;
    buildCard();
    return view;
  }

  // The line under the view: what the view itself has asked the tool since the model's call.
  function showViewCalls(last) {
    $("view-calls").textContent = last
      ? `Calls from the view to the tool: ${state.viewCalls}. Last: ${last.name} · ${argsText(last.args)}${last.note ? ` (${last.note})` : ""}`
      : "Calls from the view to the tool: none yet.";
  }

  // What a host does when the model calls the tool: pass the input, run the tool, pass the result.
  // The example request and the tool line above the view describe this call, and only this call.
  async function modelCall(args) {
    sendToView({ jsonrpc: "2.0", method: "ui/notifications/tool-input", params: { arguments: args } });
    let result;
    try {
      result = await postJson("/api/mcp_app/call", { name: SHOW_TOOL, arguments: args });
    } catch (error) {
      sendToView({ jsonrpc: "2.0", method: "ui/notifications/tool-cancelled", params: { reason: error.message } });
      showPageError(`The tool call failed: ${error.message}`);
      return;
    }
    $("mcp-text").textContent = (result.content || []).map((block) => block.text).join("\n");
    const delivered = STRIP_STRUCTURED ? { content: result.content, isError: result.isError } : result;
    sendToView({ jsonrpc: "2.0", method: "ui/notifications/tool-result", params: delivered });
    state.viewCalls = 0;
    showViewCalls(null);
    $("mcp-args").textContent = argsText(args);
    const view = followView(result);
    $("mcp-prompt").textContent = view
      ? `Show the 10-K ripple graph for ${view.book_label}: ${scenarioText(view.scenario.rate_bps, view.scenario.oil_pct)}.`
      : `Show the 10-K ripple graph (${argsText(args)}).`;
  }

  window.addEventListener("message", async (event) => {
    if (event.source !== frame.contentWindow) return;
    const message = event.data;
    if (!message || message.jsonrpc !== "2.0") return;
    logMessage("from view", message);
    const reply = (result) => sendToView({ jsonrpc: "2.0", id: message.id, result });
    switch (message.method) {
      case "ui/initialize":
        reply({
          protocolVersion: (message.params && message.params.protocolVersion) || "2026-01-26",
          hostInfo: HOST_INFO,
          hostCapabilities: OFFER_TOOLS ? { serverTools: {} } : {},
          hostContext: hostContext(),
        });
        break;
      case "ui/notifications/initialized":
        state.viewReady = true;
        await modelCall(modelArguments());
        break;
      case "tools/call": {
        if (!OFFER_TOOLS) {
          sendToView({ jsonrpc: "2.0", id: message.id, error: { code: -32601, message: "This host does not proxy server tools." } });
          break;
        }
        const name = message.params.name;
        const args = message.params.arguments || {};
        state.viewCalls += 1;
        try {
          const result = await postJson("/api/mcp_app/call", { name, arguments: args });
          reply(result);
          showViewCalls({ name, args, note: result.isError ? "the tool returned an error" : "" });
          followView(result);
        } catch (error) {
          sendToView({ jsonrpc: "2.0", id: message.id, error: { code: -32000, message: error.message } });
          showViewCalls({ name, args, note: "the call failed" });
        }
        break;
      }
      case "ui/notifications/size-changed":
        frame.style.height = `${Math.min(Math.max(Number(message.params.height) || 150, 150), INLINE_MAX_HEIGHT)}px`;
        break;
      default:
        if (message.method && message.id !== undefined) {
          sendToView({ jsonrpc: "2.0", id: message.id, error: { code: -32601, message: `Method not found: ${message.method}` } });
        }
    }
  });

  function setBooks(books) {
    if (JSON.stringify(books.map((book) => book.id)) === JSON.stringify(state.books.map((book) => book.id))) return;
    state.books = books;
    for (const id of ["mcp-book", "a2ui-book"]) {
      const select = $(id);
      const current = select.value;
      select.replaceChildren(
        ...books.map((book) => {
          const option = document.createElement("option");
          option.value = book.id;
          option.textContent = book.label;
          return option;
        }),
      );
      if (books.some((book) => book.id === current)) select.value = current;
    }
    fillTickers();
  }

  async function loadView() {
    let resource;
    try {
      resource = await getJson("/api/mcp_app/resource");
    } catch (error) {
      showPageError(
        `This workstation cannot serve the preview (${error.message}). If it was started before the preview was added, ` +
          "restart scripts/server.py and reload this page.",
      );
      return false;
    }
    frame.srcdoc = resource.text.replace("<head>", `<head>\n<meta http-equiv="Content-Security-Policy" content="${DEFAULT_CSP}">`);
    return true;
  }

  $("mcp-call").addEventListener("click", () => {
    if (state.viewReady) modelCall(modelArguments());
  });
  $("mcp-theme").addEventListener("click", () => {
    state.theme = state.theme === "light" ? "dark" : "light";
    $("mcp-theme").textContent = `Switch host theme to ${state.theme === "light" ? "dark" : "light"}`;
    $("chat-window").classList.toggle("dark", state.theme === "dark");
    if (state.viewReady) sendToView({ jsonrpc: "2.0", method: "ui/notifications/host-context-changed", params: { theme: state.theme } });
  });

  // ------------------------------------------------------------------ A2UI preview
  // What a Markdown renderer displays for a backslash-escaped punctuation character.
  const unescapeMarkdown = (text) => text.replace(/\\([!-\/:-@\[-`{-~])/g, "$1");

  function renderComponent(components, id) {
    const component = components.get(id);
    if (!component) {
      const missing = document.createElement("div");
      missing.className = "a2ui-unsupported";
      missing.textContent = `Missing component "${id}"`;
      return missing;
    }
    const children = () => (component.children || []).map((child) => renderComponent(components, child));
    let node;
    switch (component.component) {
      case "Card":
        node = document.createElement("div");
        node.className = "a2ui-card";
        node.append(renderComponent(components, component.child));
        break;
      case "Column":
        node = document.createElement("div");
        node.className = "a2ui-column";
        node.append(...children());
        break;
      case "Row":
        node = document.createElement("div");
        node.className = "a2ui-row";
        node.append(...children());
        break;
      case "Text": {
        const variant = component.variant || "body";
        node = document.createElement(/^h[1-5]$/.test(variant) ? variant : "p");
        node.className = `a2ui-text ${variant}`;
        node.textContent = typeof component.text === "string" ? unescapeMarkdown(component.text) : "[data-bound text is not supported by this preview]";
        break;
      }
      case "Divider":
        node = document.createElement("hr");
        node.className = "a2ui-divider";
        break;
      default:
        node = document.createElement("div");
        node.className = "a2ui-unsupported";
        node.textContent = `Component "${component.component}" is not supported by this preview`;
    }
    if (typeof component.weight === "number") node.style.flex = `${component.weight} 1 0`;
    node.dataset.a2uiId = id;
    return node;
  }

  // Process the messages in order, as a client must: create, update, delete.
  function renderMessages(messages, mount) {
    const surfaces = new Map();
    for (const message of messages) {
      if (message.createSurface) {
        surfaces.set(message.createSurface.surfaceId, { theme: message.createSurface.theme || {}, components: new Map() });
      } else if (message.updateComponents) {
        const surface = surfaces.get(message.updateComponents.surfaceId);
        if (!surface) throw new Error(`updateComponents names a surface that was not created: ${message.updateComponents.surfaceId}`);
        for (const component of message.updateComponents.components) surface.components.set(component.id, component);
      } else if (message.deleteSurface) {
        surfaces.delete(message.deleteSurface.surfaceId);
      }
    }
    mount.replaceChildren();
    for (const [surfaceId, surface] of surfaces) {
      const wrapper = document.createElement("div");
      wrapper.dataset.surfaceId = surfaceId;
      if (surface.theme.agentDisplayName) {
        const agent = document.createElement("div");
        agent.className = "a2ui-agent";
        agent.textContent = surface.theme.agentDisplayName;
        wrapper.append(agent);
      }
      wrapper.append(renderComponent(surface.components, "root"));
      mount.append(wrapper);
    }
  }

  const currentBook = () => state.books.find((item) => item.id === $("a2ui-book").value) || state.books[0];

  // The holdings of the chosen book. A book comes back with the holding it last showed;
  // a book not seen yet starts on United when it holds United, and on its first holding otherwise.
  function fillTickers() {
    const book = currentBook();
    const select = $("a2ui-ticker");
    const tickers = (book && book.tickers) || [];
    const wanted = (book && state.cardTicker[book.id]) || DEFAULT_TICKER;
    select.replaceChildren(
      ...tickers.map((ticker) => {
        const option = document.createElement("option");
        option.value = ticker;
        option.textContent = ticker;
        return option;
      }),
    );
    if (tickers.includes(wanted)) select.value = wanted;
    select.disabled = $("a2ui-card").value !== "holding";
    renderPicker();
  }

  // One button per holding of the book, and one for the book summary. They set the same
  // fields as the form in the developer section, so there is one source of truth.
  // The buttons are rebuilt only when the book changes, so a button keeps the keyboard focus.
  let pickerKey = "";
  function renderPicker() {
    const book = currentBook();
    const tickers = (book && book.tickers) || [];
    const picker = $("card-picker");
    const key = JSON.stringify(tickers);
    if (key !== pickerKey) {
      pickerKey = key;
      const button = (text, kind, ticker) => {
        const node = document.createElement("button");
        node.type = "button";
        node.textContent = text;
        node.dataset.kind = kind;
        if (ticker) node.dataset.ticker = ticker;
        else node.className = "book";
        node.addEventListener("click", () => chooseCard(kind, ticker));
        return node;
      };
      const label = document.createElement("span");
      label.className = "picker-label";
      label.textContent = "Card for";
      picker.replaceChildren(label, ...tickers.map((ticker) => button(ticker, "holding", ticker)), button("Whole book", "book", null));
    }
    const kind = $("a2ui-card").value;
    const current = $("a2ui-ticker").value;
    for (const node of picker.querySelectorAll("button")) {
      const pressed = node.dataset.kind === kind && (kind === "book" || node.dataset.ticker === current);
      node.setAttribute("aria-pressed", String(pressed));
    }
  }

  function chooseCard(kind, ticker) {
    $("a2ui-card").value = kind;
    $("a2ui-ticker").disabled = kind !== "holding";
    if (ticker) $("a2ui-ticker").value = ticker;
    buildCard();
  }

  // The example request for a card. It points at the graph when the card is for what the
  // view shows, and spells the book and scenario out when the developer form asked for others.
  function cardRequestText(request) {
    const shown = state.viewShows;
    const likeGraph =
      shown !== null && request.book_id === shown.book_id && String(request.rate_bps) === shown.rate_bps && String(request.oil_pct) === shown.oil_pct;
    const tail = likeGraph ? ", for the scenario in the graph." : `: ${scenarioText(request.rate_bps, request.oil_pct)}.`;
    if (request.card === "holding") return `Show ${request.ticker} as a card${tail}`;
    const book = state.books.find((item) => item.id === request.book_id);
    return `Summarise ${likeGraph ? "the whole book" : (book && book.label) || request.book_id} as a card${tail}`;
  }

  async function buildCard() {
    const seq = ++state.cardSeq;
    const card = $("a2ui-card").value;
    const ticker = card === "holding" ? $("a2ui-ticker").value || DEFAULT_TICKER : null;
    const request = {
      card,
      ticker,
      book_id: $("a2ui-book").value || DEFAULT_BOOK,
      rate_bps: $("a2ui-rate").value,
      oil_pct: $("a2ui-oil").value,
    };
    if (ticker) state.cardTicker[request.book_id] = ticker;
    $("a2ui-prompt").textContent = cardRequestText(request);
    $("a2ui-args").textContent =
      `${card === "holding" ? `holding ${ticker}` : `book ${request.book_id}`} · rate_bps ${request.rate_bps} · oil_pct ${request.oil_pct}`;
    renderPicker();
    let payload;
    try {
      payload = await postJson("/api/a2ui", request);
    } catch (error) {
      if (seq !== state.cardSeq) return;
      $("a2ui-mount").textContent = `The card could not be built: ${error.message}`;
      $("a2ui-json").textContent = "";
      $("a2ui-facts").replaceChildren();
      return;
    }
    if (seq !== state.cardSeq) return; // a newer card was asked for while this one was on its way
    renderMessages(payload.messages, $("a2ui-mount"));
    const json = JSON.stringify(payload.messages, null, 2);
    $("a2ui-json").textContent = json;
    const components = payload.messages.reduce((count, message) => count + (message.updateComponents ? message.updateComponents.components.length : 0), 0);
    const facts = [
      ["Protocol version", payload.version],
      ["Catalog", payload.catalog_id],
      ["MIME type over A2A", payload.mime_type],
      ["Surface", payload.surface_id],
      ["Size", `${payload.messages.length} messages · ${components} components · ${new Blob([JSON.stringify(payload.messages)]).size} bytes`],
    ];
    $("a2ui-facts").replaceChildren(
      ...facts.flatMap(([term, value]) => {
        const dt = document.createElement("dt");
        dt.textContent = term;
        const dd = document.createElement("dd");
        dd.textContent = value;
        return [dt, dd];
      }),
    );
  }

  $("a2ui-build").addEventListener("click", buildCard);
  $("a2ui-card").addEventListener("change", () => { fillTickers(); buildCard(); });
  $("a2ui-book").addEventListener("change", () => { fillTickers(); buildCard(); });
  $("a2ui-ticker").addEventListener("change", buildCard);

  // ------------------------------------------------------------------ start
  // The first tool result brings the book list; the first card is built when it arrives.
  if (query.get("dev") === "1") $("dev-panel").open = true;
  loadView();
})();
