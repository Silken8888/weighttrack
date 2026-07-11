// WeighTrack front end. No build step, no frameworks -- this is a personal
// single-user app, plain JS keeps it easy to pick back up later.

(function () {
  "use strict";

  /* ----------------------------------------------------------------
     Live "packed on" stamp: date + time (updates every second) and
     location (resolved once, client-side, since it doesn't change on
     every refresh). Both degrade gracefully -- this is a nice-to-have,
     never something that should block the page.
     ---------------------------------------------------------------- */

  function startClock() {
    const dateEl = document.getElementById("stamp-date");
    const timeEl = document.getElementById("stamp-time");
    if (!dateEl || !timeEl) return;

    const dateFmt = new Intl.DateTimeFormat(undefined, {
      weekday: "short",
      month: "short",
      day: "numeric",
      year: "numeric",
    });
    const timeFmt = new Intl.DateTimeFormat(undefined, {
      hour: "numeric",
      minute: "2-digit",
      second: "2-digit",
    });

    function tick() {
      const now = new Date();
      dateEl.textContent = dateFmt.format(now);
      timeEl.textContent = timeFmt.format(now);
    }

    tick();
    setInterval(tick, 1000);
  }

  function resolveLocation() {
    const locEl = document.getElementById("stamp-location");
    if (!locEl || !("geolocation" in navigator)) {
      if (locEl) locEl.textContent = "location unavailable";
      return;
    }

    navigator.geolocation.getCurrentPosition(
      function (position) {
        const { latitude, longitude } = position.coords;
        const url =
          "https://api.bigdatacloud.net/data/reverse-geocode-client?latitude=" +
          latitude +
          "&longitude=" +
          longitude +
          "&localityLanguage=en";

        fetch(url)
          .then(function (res) {
            if (!res.ok) throw new Error("reverse geocode failed");
            return res.json();
          })
          .then(function (data) {
            const city = data.city || data.locality || "";
            const region = data.principalSubdivisionCode
              ? data.principalSubdivisionCode.split("-").pop()
              : "";
            locEl.textContent = [city, region].filter(Boolean).join(", ") || "location found";
          })
          .catch(function () {
            locEl.textContent = "location unavailable";
          });
      },
      function () {
        locEl.textContent = "location not shared";
      },
      { timeout: 8000 }
    );
  }

  /* ----------------------------------------------------------------
     Food search: POST /food/search enqueues a background job and
     returns a job_id immediately; we poll /food/search/status/<id>
     until it's done. The search route never blocks on Open Food Facts
     itself, so this poll is the only place that "waits."
     ---------------------------------------------------------------- */

  function initSearch() {
    const form = document.getElementById("search-form");
    const input = document.getElementById("search-input");
    const statusEl = document.getElementById("search-status");
    const resultsEl = document.getElementById("search-results");
    if (!form || !input || !statusEl || !resultsEl) return;

    form.addEventListener("submit", function (evt) {
      evt.preventDefault();
      const query = input.value.trim();
      if (!query) return;
      runSearch(query, statusEl, resultsEl);
    });
  }

  function initPhotoSearch() {
    const fileInput = document.getElementById("search-photo-input");
    const statusEl = document.getElementById("search-status");
    const resultsEl = document.getElementById("search-results");
    if (!fileInput || !statusEl || !resultsEl) return;

    fileInput.addEventListener("change", function () {
      const file = fileInput.files && fileInput.files[0];
      if (!file) return;
      runPhotoSearch(file, statusEl, resultsEl);
      fileInput.value = ""; // allow re-selecting the same file later
    });
  }

  function runSearch(query, statusEl, resultsEl) {
    resultsEl.innerHTML = "";
    setStatus(statusEl, "pending", "Searching Open Food Facts for \u201c" + query + "\u201d\u2026");

    fetch("/food/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query: query, page_size: 10 }),
    })
      .then(function (res) {
        if (!res.ok) return res.json().then(function (b) { throw new Error(b.error || "Search failed"); });
        return res.json();
      })
      .then(function (data) {
        pollSearch(data.job_id, statusEl, resultsEl, 0);
      })
      .catch(function (err) {
        setStatus(statusEl, "error", err.message || "Couldn't start the search.");
      });
  }

  function runPhotoSearch(file, statusEl, resultsEl) {
    resultsEl.innerHTML = "";
    setStatus(statusEl, "pending", "Reading barcode and label text from the photo\u2026");

    const formData = new FormData();
    formData.append("photo", file);

    fetch("/food/search-photo", { method: "POST", body: formData })
      .then(function (res) {
        if (!res.ok) return res.json().then(function (b) { throw new Error(b.error || "Photo search failed"); });
        return res.json();
      })
      .then(function (data) {
        pollSearch(data.job_id, statusEl, resultsEl, 0);
      })
      .catch(function (err) {
        setStatus(statusEl, "error", err.message || "Couldn't read that photo.");
      });
  }

  function pollSearch(jobId, statusEl, resultsEl, attempt) {
    const MAX_ATTEMPTS = 30; // ~ 20s at 650ms/attempt

    fetch("/food/search/status/" + jobId)
      .then(function (res) { return res.json(); })
      .then(function (job) {
        if (job.status === "pending") {
          if (attempt >= MAX_ATTEMPTS) {
            setStatus(statusEl, "error", "This is taking longer than expected -- try again.");
            return;
          }
          setTimeout(function () {
            pollSearch(jobId, statusEl, resultsEl, attempt + 1);
          }, 650);
          return;
        }

        if (job.status === "error") {
          setStatus(statusEl, "error", job.error || "Search failed.");
          return;
        }

        renderResults(job.results || [], job.note, statusEl, resultsEl);
      })
      .catch(function () {
        setStatus(statusEl, "error", "Lost track of that search -- try again.");
      });
  }

  function setStatus(statusEl, state, text) {
    statusEl.dataset.state = state;
    statusEl.textContent = text;
  }

  function renderResults(results, note, statusEl, resultsEl) {
    if (results.length === 0) {
      setStatus(statusEl, "done", note || "No matches. Try a shorter or more generic search term.");
      return;
    }

    let text = results.length + " match" + (results.length === 1 ? "" : "es") + " -- confirm one to save it.";
    if (note) text = note + " " + text;
    setStatus(statusEl, "done", text);

    results.forEach(function (product, idx) {
      resultsEl.appendChild(buildResultRow(product, idx));
    });
  }

  function buildResultRow(product, idx) {
    const li = document.createElement("li");
    li.className = "result-row";

    const photo = document.createElement("img");
    photo.className = "result-row__photo";
    photo.alt = "";
    photo.src = product.photo_url || "";
    if (!product.photo_url) photo.style.visibility = "hidden";
    li.appendChild(photo);

    const info = document.createElement("div");
    const name = document.createElement("div");
    name.className = "result-row__name";
    name.textContent = product.product_name;
    info.appendChild(name);

    const meta = document.createElement("div");
    meta.className = "result-row__meta";
    const bits = [];
    if (product.brand) bits.push(product.brand);
    if (product.calories != null) bits.push(Math.round(product.calories) + " cal");
    meta.textContent = bits.join(" \u00b7 ");
    info.appendChild(meta);

    if (product.basis === "100g") {
      const basis = document.createElement("span");
      basis.className = "result-row__basis";
      basis.textContent = "per 100g -- confirm a serving size below";
      info.appendChild(basis);
    }
    li.appendChild(info);

    const openBtn = document.createElement("button");
    openBtn.type = "button";
    openBtn.className = "btn btn--ghost";
    openBtn.textContent = "Save";
    li.appendChild(openBtn);

    const confirmForm = buildConfirmForm(product, idx);
    li.appendChild(confirmForm);

    openBtn.addEventListener("click", function () {
      confirmForm.classList.toggle("is-open");
    });

    return li;
  }

  function buildConfirmForm(product, idx) {
    const form = document.createElement("form");
    form.className = "confirm-form";

    const nickLabel = document.createElement("label");
    nickLabel.textContent = "Nickname";
    const nickInput = document.createElement("input");
    nickInput.type = "text";
    nickInput.required = true;
    nickInput.placeholder = "e.g. my oatmeal";
    nickLabel.appendChild(nickInput);
    form.appendChild(nickLabel);

    let scaleInput = null;
    if (product.basis === "100g") {
      const scaleLabel = document.createElement("label");
      scaleLabel.textContent = "Serving size (g)";
      scaleInput = document.createElement("input");
      scaleInput.type = "number";
      scaleInput.min = "1";
      scaleInput.step = "1";
      scaleInput.placeholder = "100";
      scaleLabel.appendChild(scaleInput);
      form.appendChild(scaleLabel);
    }

    const saveBtn = document.createElement("button");
    saveBtn.type = "submit";
    saveBtn.className = "btn";
    saveBtn.textContent = "Confirm & add";
    form.appendChild(saveBtn);

    const rowStatus = document.createElement("span");
    rowStatus.className = "result-row__meta";
    form.appendChild(rowStatus);

    form.addEventListener("submit", function (evt) {
      evt.preventDefault();
      const nickname = nickInput.value.trim();
      if (!nickname) return;

      let payload = Object.assign({}, product, { nickname: nickname });
      delete payload.basis;

      if (scaleInput && scaleInput.value) {
        const grams = parseFloat(scaleInput.value);
        if (grams > 0) {
          const factor = grams / 100;
          ["calories", "protein_g", "carbs_g", "fat_g", "fiber_g", "sugar_g", "sodium_mg"].forEach(function (key) {
            if (payload[key] != null) payload[key] = Math.round(payload[key] * factor * 10) / 10;
          });
          payload.serving_description = grams + " g";
        }
      }

      saveBtn.disabled = true;
      rowStatus.textContent = "Saving\u2026";

      fetch("/food/add", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      })
        .then(function (res) {
          return res.json().then(function (body) {
            if (!res.ok) throw new Error(body.error || "Couldn't save that item.");
            return body;
          });
        })
        .then(function () {
          window.location.reload();
        })
        .catch(function (err) {
          saveBtn.disabled = false;
          rowStatus.textContent = err.message;
        });
    });

    return form;
  }

  /* ----------------------------------------------------------------
     Nutrition Facts modal
     ---------------------------------------------------------------- */

  const DAILY_VALUES = { fat_g: 78, sodium_mg: 2300, carbs_g: 275, fiber_g: 28 };

  function pctDV(key, value) {
    if (value == null || !DAILY_VALUES[key]) return null;
    return Math.round((value / DAILY_VALUES[key]) * 100);
  }

  function initNutritionModal() {
    const backdrop = document.getElementById("nutrition-modal");
    if (!backdrop) return;
    const closeBtn = document.getElementById("nutrition-modal-close");

    document.querySelectorAll("[data-open-nutrition]").forEach(function (card) {
      card.addEventListener("click", function () {
        openNutritionModal(backdrop, card.dataset);
      });
    });

    function close() {
      backdrop.classList.remove("is-open");
    }

    closeBtn.addEventListener("click", close);
    backdrop.addEventListener("click", function (evt) {
      if (evt.target === backdrop) close();
    });
    document.addEventListener("keydown", function (evt) {
      if (evt.key === "Escape") close();
    });
  }

  function openNutritionModal(backdrop, data) {
    const num = function (v) { return v === "" || v == null ? null : parseFloat(v); };
    const calories = num(data.calories);
    const protein = num(data.protein);
    const carbs = num(data.carbs);
    const fat = num(data.fat);
    const fiber = num(data.fiber);
    const sugar = num(data.sugar);
    const sodium = num(data.sodium);

    set("nl-name", data.name);
    set("nl-serving", data.serving || "1 serving");
    set("nl-cals", calories != null ? Math.round(calories) : "--");

    setRow("fat", fat, "g", pctDV("fat_g", fat));
    setRow("sodium", sodium, "mg", pctDV("sodium_mg", sodium));
    setRow("carbs", carbs, "g", pctDV("carbs_g", carbs));
    setRow("fiber", fiber, "g", pctDV("fiber_g", fiber));
    setRow("sugar", sugar, "g", null);
    setRow("protein", protein, "g", null);

    backdrop.classList.add("is-open");
  }

  function set(id, value) {
    const el = document.getElementById(id);
    if (el) el.textContent = value == null ? "--" : value;
  }

  function setRow(key, value, unit, dv) {
    const valueEl = document.getElementById("nl-" + key + "-val");
    const dvEl = document.getElementById("nl-" + key + "-dv");
    if (valueEl) valueEl.textContent = value == null ? "--" : value + unit;
    if (dvEl) dvEl.textContent = dv == null ? "" : dv + "%";
  }

  /* ----------------------------------------------------------------
     Delete
     ---------------------------------------------------------------- */

  function initDelete() {
    document.querySelectorAll("[data-delete-id]").forEach(function (btn) {
      btn.addEventListener("click", function (evt) {
        evt.stopPropagation();
        const id = btn.dataset.deleteId;
        const name = btn.dataset.deleteName || "this item";
        if (!window.confirm("Remove " + name + " from your food library?")) return;

        fetch("/food/" + id + "/delete", { method: "POST" })
          .then(function (res) {
            if (!res.ok) throw new Error("delete failed");
            window.location.reload();
          })
          .catch(function () {
            window.alert("Couldn't remove that item -- try again.");
          });
      });
    });
  }

  /* ----------------------------------------------------------------
     Timeline: log a food-library item to today's timeline
     ---------------------------------------------------------------- */

  function initLogging() {
    document.querySelectorAll("[data-open-log]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        const form = document.querySelector('[data-log-form-for="' + btn.dataset.logItemId + '"]');
        if (form) form.classList.toggle("is-open");
      });
    });

    document.querySelectorAll("[data-log-form-for]").forEach(function (form) {
      form.addEventListener("submit", function (evt) {
        evt.preventDefault();
        const itemId = form.dataset.logFormFor;
        const mealType = form.querySelector('[name="meal_type"]').value;
        const servings = form.querySelector('[name="servings"]').value || 1;
        const submitBtn = form.querySelector('button[type="submit"]');

        submitBtn.disabled = true;

        fetch("/log/add", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ food_item_id: Number(itemId), meal_type: mealType, servings: Number(servings) }),
        })
          .then(function (res) {
            return res.json().then(function (body) {
              if (!res.ok) throw new Error(body.error || "Couldn't log that item.");
              return body;
            });
          })
          .then(function () {
            window.location.reload();
          })
          .catch(function (err) {
            submitBtn.disabled = false;
            window.alert(err.message);
          });
      });
    });
  }

  function initTimelineDelete() {
    document.querySelectorAll("[data-delete-log-id]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        const id = btn.dataset.deleteLogId;
        fetch("/log/" + id + "/delete", { method: "POST" })
          .then(function (res) {
            if (!res.ok) throw new Error("delete failed");
            window.location.reload();
          })
          .catch(function () {
            window.alert("Couldn't remove that entry -- try again.");
          });
      });
    });
  }

  /* ----------------------------------------------------------------
     Meal photo logging: snap a photo, Claude gives a rough calorie
     estimate, the whole thing lands directly on the timeline. The
     calorie number can be corrected afterward -- see initCalorieAdjust.
     ---------------------------------------------------------------- */

  function initMealPhotoLogging() {
    const form = document.getElementById("meal-photo-form");
    const statusEl = document.getElementById("meal-photo-status");
    if (!form || !statusEl) return;

    const defaultStatusText = statusEl.textContent;

    form.addEventListener("submit", function (evt) {
      evt.preventDefault();

      const fileInput = document.getElementById("meal-photo-input");
      const file = fileInput.files && fileInput.files[0];
      if (!file) {
        setStatus(statusEl, "error", "Choose a photo first.");
        return;
      }

      const formData = new FormData();
      formData.append("photo", file);
      formData.append("meal_type", document.getElementById("meal-photo-meal-type").value);
      formData.append("servings", document.getElementById("meal-photo-servings").value || "1");
      const description = document.getElementById("meal-photo-description").value.trim();
      if (description) formData.append("description", description);

      setStatus(statusEl, "pending", "Uploading photo and estimating calories\u2026");

      fetch("/log/photo", { method: "POST", body: formData })
        .then(function (res) {
          if (!res.ok) return res.json().then(function (b) { throw new Error(b.error || "Couldn't log that meal."); });
          return res.json();
        })
        .then(function (data) {
          pollMealPhoto(data.job_id, statusEl, defaultStatusText, 0);
        })
        .catch(function (err) {
          setStatus(statusEl, "error", err.message || "Couldn't start that upload.");
        });
    });
  }

  function pollMealPhoto(jobId, statusEl, defaultStatusText, attempt) {
    const MAX_ATTEMPTS = 30;

    fetch("/food/search/status/" + jobId)
      .then(function (res) { return res.json(); })
      .then(function (job) {
        if (job.status === "pending") {
          if (attempt >= MAX_ATTEMPTS) {
            setStatus(statusEl, "error", "This is taking longer than expected -- check your timeline shortly.");
            return;
          }
          setTimeout(function () {
            pollMealPhoto(jobId, statusEl, defaultStatusText, attempt + 1);
          }, 800);
          return;
        }

        if (job.status === "error") {
          setStatus(statusEl, "error", job.error || "Couldn't log that meal.");
          return;
        }

        // job.note carries a non-fatal AI-estimate error even on success
        // (photo saved, but no calorie guess) -- reload either way so the
        // new timeline entry shows up, the note isn't worth blocking on.
        window.location.reload();
      })
      .catch(function () {
        setStatus(statusEl, "error", "Lost track of that upload -- check your timeline.");
      });
  }

  function initCalorieAdjust() {
    document.querySelectorAll("[data-adjust-log-id]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        const id = btn.dataset.adjustLogId;
        const current = btn.dataset.adjustCurrent || "";
        const next = window.prompt("Adjust the calorie estimate for this meal:", current);
        if (next === null) return;

        const calories = parseFloat(next);
        if (isNaN(calories) || calories < 0) {
          window.alert("Enter a valid number of calories.");
          return;
        }

        fetch("/log/" + id + "/adjust", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ calories: calories }),
        })
          .then(function (res) {
            if (!res.ok) throw new Error("adjust failed");
            window.location.reload();
          })
          .catch(function () {
            window.alert("Couldn't update that -- try again.");
          });
      });
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    startClock();
    resolveLocation();
    initSearch();
    initPhotoSearch();
    initNutritionModal();
    initDelete();
    initLogging();
    initTimelineDelete();
    initMealPhotoLogging();
    initCalorieAdjust();
  });
})();
