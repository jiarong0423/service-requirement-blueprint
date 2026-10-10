"use strict";

document.documentElement.classList.add("js-enabled");

document.addEventListener("invalid", (event) => {
  const control = event.target;
  if (!(control instanceof HTMLElement)) {
    return;
  }
  let disclosure = control.closest("details");
  while (disclosure) {
    disclosure.open = true;
    disclosure = disclosure.parentElement?.closest("details") ?? null;
  }
}, true);

for (const form of document.querySelectorAll("form[data-requires-visual-choice]")) {
  const style = form.elements.namedItem("brand_style");
  const excluded = form.elements.namedItem("excluded_directions");
  if (!(style instanceof HTMLTextAreaElement) || !(excluded instanceof HTMLTextAreaElement)) {
    continue;
  }

  const validate = () => {
    style.setCustomValidity(
      style.value.trim() || excluded.value.trim()
        ? ""
        : "Add a brand style or a direction to avoid."
    );
  };

  style.addEventListener("input", validate);
  excluded.addEventListener("input", validate);
  form.addEventListener("submit", (event) => {
    validate();
    if (!form.reportValidity()) {
      event.preventDefault();
    }
  });
}
