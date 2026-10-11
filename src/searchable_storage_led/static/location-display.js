(() => {
  function rawLabel(location) {
    if (typeof location === "string") return location;
    return location?.label || location?.id || "";
  }

  function normalizedLabel(location) {
    return rawLabel(location).trim().replace(/\s+/g, " ").toLowerCase();
  }

  function catalogLabel(location, catalog = []) {
    if (!location || typeof location === "string") return rawLabel(location);
    if (location.display_label) return location.display_label;
    const label = rawLabel(location);
    const active = location.active !== false;
    const ambiguous = catalog.some((candidate) =>
      candidate
      && candidate.id !== location.id
      && candidate.parent_id === location.parent_id
      && (candidate.active !== false) === active
      && normalizedLabel(candidate) === normalizedLabel(location)
    );
    return ambiguous ? `${label} · ${location.id}` : label;
  }

  function pathText(path = [], catalog = [], separator = " > ") {
    return (Array.isArray(path) ? path : [])
      .map((node) => catalogLabel(node, catalog))
      .filter(Boolean)
      .join(separator);
  }

  window.MagpieLocationDisplay = {catalogLabel, pathText, rawLabel};
})();
