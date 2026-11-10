(function () {
  const root = document.getElementById("purpose-analytics");
  if (!root) return;

  const form = document.getElementById("purpose-analytics-filters");
  const period = document.getElementById("purpose-analytics-period");
  const chart = root.querySelector("[data-purpose-chart]");
  const table = root.querySelector("[data-purpose-table]");
  const total = root.querySelector("[data-purpose-total]");
  const leader = root.querySelector("[data-purpose-leader]");
  const leaderCount = root.querySelector("[data-purpose-leader-count]");
  const error = root.querySelector("[data-purpose-error]");
  const svgNamespace = "http://www.w3.org/2000/svg";
  let latestRequest = 0;
  const filterNames = [
    "period", "daily_date", "month", "year",
    "analytics_start", "analytics_end", "document_type", "office"
  ];

  function showError(message) {
    error.textContent = message || "";
    error.hidden = !message;
  }

  function updatePeriodFields() {
    root.querySelectorAll(".purpose-analytics-period-field").forEach(function (field) {
      const active = field.dataset.period === period.value;
      field.hidden = !active;
      field.querySelectorAll("input").forEach(function (input) {
        input.disabled = !active;
        input.required = active;
      });
    });
  }

  function svgNode(name, attributes, text) {
    const node = document.createElementNS(svgNamespace, name);
    Object.keys(attributes).forEach(function (attribute) {
      node.setAttribute(attribute, attributes[attribute]);
    });
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function renderChart(data) {
    chart.replaceChildren();
    if (!data.items.length) {
      const empty = document.createElement("div");
      empty.className = "empty-state report-analytics-empty";
      empty.textContent = "No requests match this period.";
      chart.appendChild(empty);
      return;
    }

    const width = Math.max(640, 90 + data.items.length * 100);
    const svg = svgNode("svg", {
      viewBox: "0 0 " + width + " 360",
      width: width,
      height: "360",
      role: "img",
      "aria-label": root.dataset.chartLabel +
        "; x-axis: Request Purpose/Reason; y-axis: Number of Requests"
    });
    [0, 0.5, 1].forEach(function (fraction) {
      const y = 260 - 210 * fraction;
      svg.appendChild(svgNode("line", {
        x1: "48",
        y1: y,
        x2: width - 12,
        y2: y,
        class: "report-chart-gridline"
      }));
      svg.appendChild(svgNode("text", {
        x: "40",
        y: y + 4,
        "text-anchor": "end",
        class: "report-chart-tick"
      }, String(Math.round(data.chart_max * fraction))));
    });

    data.items.forEach(function (item, index) {
      const x = 62 + index * 100;
      const barHeight = Math.max(1, Math.round(item.count / data.chart_max * 210));
      const y = 260 - barHeight;
      const labelX = x + 23;
      const labelY = 279;
      const group = svgNode("g", { class: "report-chart-bar" });
      group.appendChild(svgNode("title", {}, item.purpose + ": " + item.count + " requests"));
      group.appendChild(svgNode("rect", {
        x: x,
        y: y,
        width: "46",
        height: barHeight,
        rx: "5",
        fill: item.color
      }));
      group.appendChild(svgNode("text", {
        x: labelX,
        y: y - 8,
        "text-anchor": "middle",
        class: "report-chart-value"
      }, String(item.count)));
      group.appendChild(svgNode("text", {
        x: labelX,
        y: labelY,
        transform: "rotate(45 " + labelX + " " + labelY + ")",
        class: "report-chart-label"
      }, item.purpose));
      svg.appendChild(group);
    });
    chart.appendChild(svg);
  }

  function renderTable(items) {
    table.replaceChildren();
    if (!items.length) {
      const row = document.createElement("tr");
      row.className = "report-analytics-table-empty";
      const cell = document.createElement("td");
      cell.colSpan = 2;
      cell.textContent = "No matching records.";
      row.appendChild(cell);
      table.appendChild(row);
      return;
    }
    items.forEach(function (item) {
      const row = document.createElement("tr");
      const label = document.createElement("th");
      label.scope = "row";
      label.textContent = item.purpose;
      const count = document.createElement("td");
      count.textContent = String(item.count);
      row.append(label, count);
      table.appendChild(row);
    });
  }

  async function loadAnalytics() {
    const requestId = ++latestRequest;
    const formData = new FormData(form);
    const query = new URLSearchParams();
    filterNames.forEach(function (key) {
      if (formData.has(key)) query.set(key, formData.get(key));
    });
    const response = await fetch(root.dataset.dataUrl + "?" + query.toString(), {
      headers: { Accept: "application/json" }
    });
    const data = await response.json();
    if (requestId !== latestRequest) return;
    if (!response.ok) {
      showError(data.error || "Unable to update purpose analytics.");
      return;
    }
    showError("");
    total.textContent = String(data.total);
    leader.textContent = data.most_common_purpose || root.dataset.noLeader;
    leaderCount.textContent = data.most_common_purpose
      ? String(data.most_common_purpose_count) + " requests"
      : "";
    leaderCount.hidden = !data.most_common_purpose;
    renderChart(data);
    renderTable(data.items);
  }

  function refresh() {
    if (!form.checkValidity()) return;
    loadAnalytics().catch(function () {
      showError("Unable to load purpose analytics. Please try again.");
    });
  }

  period.addEventListener("change", function () {
    updatePeriodFields();
  });
  form.addEventListener("change", refresh);
  form.addEventListener("submit", function (event) {
    event.preventDefault();
    if (!form.reportValidity()) return;
    const button = form.querySelector('button[type="submit"]');
    button.disabled = true;
    loadAnalytics().catch(function () {
      showError("Unable to load purpose analytics. Please try again.");
    }).finally(function () {
      button.disabled = false;
    });
  });
  updatePeriodFields();
})();
