// Live chat client for specs/message-wall.md. Renders all server-supplied
// text via textContent (never innerHTML) so posted messages can never be
// interpreted as HTML.
(function () {
  var messagesEl = document.getElementById("chat-messages");
  var nameEl = document.getElementById("chat-name");
  var visitorsEl = document.getElementById("chat-visitors");
  var statusEl = document.getElementById("chat-status");
  var errorEl = document.getElementById("chat-error");
  var form = document.getElementById("chat-form");
  var input = document.getElementById("chat-input");
  var errorTimer = null;

  function renderMessage(msg) {
    var li = document.createElement("li");
    var name = document.createElement("span");
    name.className = "chat-name";
    name.textContent = msg.name;
    li.appendChild(name);
    li.appendChild(document.createTextNode(": "));
    li.appendChild(document.createTextNode(msg.text));
    messagesEl.appendChild(li);
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function renderMessages(messages) {
    messagesEl.textContent = "";
    messages.forEach(renderMessage);
  }

  function showError(reason) {
    var text = {
      rate_limited: "You're posting too fast — wait a moment and try again.",
      too_long: "Message too long (max 500 characters).",
      bad_request: "That message couldn't be sent.",
    }[reason] || "That message couldn't be sent.";
    errorEl.textContent = text;
    if (errorTimer) {
      clearTimeout(errorTimer);
    }
    errorTimer = setTimeout(function () {
      errorEl.textContent = "";
    }, 4000);
  }

  function loadHistory() {
    fetch("/api/messages")
      .then(function (res) {
        return res.json();
      })
      .then(renderMessages)
      .catch(function () {});
  }

  function connect() {
    var proto = location.protocol === "https:" ? "wss:" : "ws:";
    var ws = new WebSocket(proto + "//" + location.host + "/ws");

    ws.onopen = function () {
      statusEl.textContent = "Connected.";
    };

    ws.onmessage = function (event) {
      var data;
      try {
        data = JSON.parse(event.data);
      } catch (err) {
        return;
      }
      if (data.type === "welcome") {
        nameEl.textContent = data.name;
        visitorsEl.textContent = data.visitors;
        renderMessages(data.messages);
      } else if (data.type === "message") {
        renderMessage(data);
      } else if (data.type === "visitors") {
        visitorsEl.textContent = data.count;
      } else if (data.type === "error") {
        showError(data.reason);
      }
    };

    ws.onclose = function () {
      statusEl.textContent = "Disconnected — reload to reconnect.";
    };

    form.addEventListener("submit", function (event) {
      event.preventDefault();
      var text = input.value;
      if (!text || ws.readyState !== WebSocket.OPEN) {
        return;
      }
      ws.send(JSON.stringify({ type: "post", text: text }));
      input.value = "";
    });
  }

  loadHistory();
  connect();
})();
