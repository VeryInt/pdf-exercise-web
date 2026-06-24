const browserTimezone = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
const currentUrl = new URL(window.location.href);

if (currentUrl.searchParams.get("timezone") !== browserTimezone) {
  currentUrl.searchParams.set("timezone", browserTimezone);
  window.location.replace(currentUrl.toString());
} else {
  const formatter = new Intl.DateTimeFormat("zh-CN", {
    timeZone: browserTimezone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
  });

  document.querySelectorAll(".local-time[data-utc]").forEach((element) => {
    const date = new Date(element.dataset.utc);
    if (!Number.isNaN(date.getTime())) {
      element.textContent = formatter.format(date).replaceAll("/", "-");
      element.title = browserTimezone;
    }
  });
}
