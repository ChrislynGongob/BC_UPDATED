(function () {
  const root = document.getElementById("admin-report-analytics");
  if (!root) return;

  const filterForm = document.getElementById("report-analytics-filters");
  const period = document.getElementById("report-analytics-period");
  const chartContainer = root.querySelector("[data-analytics-chart]");
  const colorList = root.querySelector("[data-analytics-colors]");
  const tableBody = root.querySelector("[data-analytics-table]");
  const totalNode = root.querySelector("[data-analytics-total]");
  const leadersNode = root.querySelector("[data-analytics-leaders]");
  const errorNode = root.querySelector("[data-analytics-error]");
  const colorStatus = root.querySelector("[data-analytics-color-status]");
  const svgNamespace = "http://www.w3.org/2000/svg";

  function showError(message) {
    errorNode.textContent = message || "";
    errorNode.hidden = !message;
  }

  function updatePeriodFields() {
    root.querySelectorAll(".report-analytics-period-field").forEach(function (field) {
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
    chartContainer.replaceChildren();
    if (!data.items.length) {
      const empty = document.createElement("div");
      empty.className = "empty-state report-analytics-empty";
      empty.textContent = "No records match this period.";
      chartContainer.appendChild(empty);
      return;
    }

    const width = Math.max(640, 90 + data.items.length * 100);
    const svg = svgNode("svg", {
      viewBox: "0 0 " + width + " 360",
      width: width,
      height: "360",
      role: "img",
      "aria-label": root.dataset.chartLabel
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
      const group = svgNode("g", {
        class: "report-chart-bar",
        "data-label": item.label
      });
      group.appendChild(svgNode("title", {}, item.label + ": " + item.count));
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
      }, item.label));
      svg.appendChild(group);
    });
    chartContainer.appendChild(svg);
  }

  function renderColors(items) {
    colorList.replaceChildren();
    if (!items.length) {
      const empty = document.createElement("p");
      empty.className = "muted";
      empty.textContent = "No chart colors to customize.";
      colorList.appendChild(empty);
      return;
    }
    items.forEach(function (item) {
      const label = document.createElement("label");
      label.className = "report-analytics-color-item";
      const input = document.createElement("input");
      input.type = "color";
      input.value = item.color;
      input.dataset.label = item.label;
      input.setAttribute("aria-label", "Color for " + item.label);
      input.addEventListener("input", updateChartColor);
      const name = document.createElement("span");
      name.textContent = item.label;
      label.append(input, name);
      colorList.appendChild(label);
    });
  }

  function renderTable(items) {
    tableBody.replaceChildren();
    if (!items.length) {
      const row = document.createElement("tr");
      row.className = "report-analytics-table-empty";
      const cell = document.createElement("td");
      cell.colSpan = 2;
      cell.textContent = "No matching records.";
      row.appendChild(cell);
      tableBody.appendChild(row);
      return;
    }
    items.forEach(function (item) {
      const row = document.createElement("tr");
      const label = document.createElement("th");
      label.scope = "row";
      label.textContent = item.label;
      const count = document.createElement("td");
      count.textContent = String(item.count);
      row.append(label, count);
      tableBody.appendChild(row);
    });
  }

  function updatePrintFilter(key, value) {
    const node = document.querySelector('[data-print-filter="' + key + '"]');
    if (node) node.textContent = value;
  }

  function selectedPeriodLabel(formData) {
    const period = formData.get("period");
    if (period === "daily") return formData.get("daily_date") || "";
    if (period === "monthly") return formData.get("month") || "";
    if (period === "yearly") return formData.get("year") || "";
    if (period === "custom") {
      return (formData.get("analytics_start") || "") + " to " +
        (formData.get("analytics_end") || "");
    }
    return period || "";
  }

  function updateChartColor(event) {
    const label = event.currentTarget.dataset.label;
    const group = Array.from(chartContainer.querySelectorAll(".report-chart-bar"))
      .find(function (node) { return node.dataset.label === label; });
    const rectangle = group && group.querySelector("rect");
    if (rectangle) rectangle.setAttribute("fill", event.currentTarget.value);
  }

  async function loadAnalytics() {
    const formData = new FormData(filterForm);
    const query = new URLSearchParams();
    ["period", "daily_date", "month", "year", "analytics_start", "analytics_end"]
      .forEach(function (key) {
        if (formData.has(key)) query.set(key, formData.get(key));
      });
    showError("");
    const response = await fetch(root.dataset.dataUrl + "?" + query.toString(), {
      headers: { Accept: "application/json" }
    });
    const data = await response.json();
    if (!response.ok) {
      showError(data.error || "Unable to update analytics.");
      return;
    }
    updatePrintFilter("module-period", selectedPeriodLabel(formData));
    totalNode.textContent = String(data.total);
    leadersNode.textContent = data.leaders.length
      ? data.leaders.join(", ")
      : root.dataset.noLeaders;
    renderChart(data);
    renderColors(data.color_preferences);
    renderTable(data.items);
  }

  async function saveColors(mode) {
    colorStatus.textContent = "";
    showError("");
    const colors = Object.create(null);
    colorList.querySelectorAll("input[data-label]").forEach(function (input) {
      colors[input.dataset.label] = input.value;
    });
    const response = await fetch(root.dataset.colorsUrl, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json",
        "X-CSRF-Token": document.querySelector('meta[name="csrf-token"]')?.content || "",
      },
      body: JSON.stringify({
        csrf_token: root.dataset.csrfToken,
        mode: mode,
        colors: mode === "save" ? colors : {}
      })
    });
    const data = await response.json();
    if (!response.ok) {
      showError(data.error || "Unable to save chart colors.");
      return;
    }
    colorStatus.textContent = data.message;
    try {
      await loadAnalytics();
    } catch (error) {
      showError("Colors were saved, but the chart could not be refreshed.");
    }
  }

  period.addEventListener("change", updatePeriodFields);
  filterForm.addEventListener("submit", function (event) {
    event.preventDefault();
    if (!filterForm.reportValidity()) return;
    const button = filterForm.querySelector('button[type="submit"]');
    button.disabled = true;
    loadAnalytics().catch(function () {
      showError("Unable to load analytics. Please try again.");
    }).finally(function () {
      button.disabled = false;
    });
  });
  root.querySelector("[data-analytics-save]").addEventListener("click", function () {
    saveColors("save").catch(function () {
      showError("Unable to save chart colors. Please try again.");
    });
  });
  root.querySelector("[data-analytics-reset]").addEventListener("click", function () {
    saveColors("reset").catch(function () {
      showError("Unable to reset chart colors. Please try again.");
    });
  });
  colorList.querySelectorAll("input[data-label]").forEach(function (input) {
    input.addEventListener("input", updateChartColor);
  });
  updatePeriodFields();
})();
