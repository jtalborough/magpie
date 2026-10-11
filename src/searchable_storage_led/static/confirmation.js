(() => {
  "use strict";

  function rows(presentation) {
    return Array.isArray(presentation?.rows) ? presentation.rows : [];
  }

  function row(presentation, index) {
    return rows(presentation).find(
      (candidate) => Number(candidate?.index) === Number(index)
    ) || null;
  }

  function actionLabel(presentation, actionId, fallback) {
    const action = Array.isArray(presentation?.actions)
      ? presentation.actions.find((candidate) => candidate?.id === actionId)
      : null;
    return String(action?.label || fallback || "");
  }

  function explanation(presentation) {
    const value = presentation?.explanation;
    const summary = String(value?.summary || "").trim();
    if (!summary) return null;
    return {
      label: String(value?.label || "Why this location?"),
      summary,
    };
  }

  function changePrompts(presentation) {
    const prompts = Array.isArray(presentation?.change_prompts)
      ? presentation.change_prompts
      : [];
    return prompts
      .map((prompt) => ({
        id: String(prompt?.id || "").trim(),
        label: String(prompt?.label || "").trim(),
        text: String(prompt?.text || "").trim(),
      }))
      .filter((prompt) => prompt.id && prompt.label && prompt.text)
      .slice(0, 4);
  }

  function lightLabel(rowValue, fallbackColor = null) {
    const color = rowValue?.color || fallbackColor;
    if (!color) return "No light";
    const name = String(color.name || "Color");
    if (!rowValue?.light?.location_id) return `${name} · No light`;
    if (rowValue?.light?.status === "lit") return `${name} · Lit`;
    if (rowValue?.light?.status === "unavailable") {
      return `${name} · Light did not respond`;
    }
    return `${name} · Ready to light`;
  }

  window.MagpieConfirmation = Object.freeze({
    actionLabel,
    changePrompts,
    explanation,
    lightLabel,
    row,
    rows,
  });
})();
