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

  /* ----------------------------------------------------------------
     Timezone detection: sets a cookie the server reads for every
     "today" boundary calculation (timeline, streaks, exercise history,
     the weight chart's day buckets). Without this, the whole
     server-side timezone fix silently falls back to UTC and the
     original bug (evening entries bleeding into "today" a day early)
     comes right back. Runs once per session unless the detected zone
     actually changes (e.g. travel), to avoid pointless repeat writes.
     ---------------------------------------------------------------- */

  function resolveTimezone() {
    try {
      const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
      if (!tz) return;
      const existing = document.cookie.split("; ").find(function (row) {
        return row.indexOf("wt_tz=") === 0;
      });
      const current = existing ? decodeURIComponent(existing.split("=")[1]) : null;
      if (current === tz) return;
      document.cookie = "wt_tz=" + encodeURIComponent(tz) + "; path=/; max-age=31536000; SameSite=Lax";
      // The very first time this fires, prior page state was rendered
      // against UTC (or whatever the old cookie said) -- reload once so
      // this page's own "today" boundary reflects the just-set cookie
      // instead of waiting for the next natural navigation.
      window.location.reload();
    } catch (e) {
      // Intl.DateTimeFormat is universally supported in evergreen
      // browsers -- if this somehow throws, just leave the server on
      // its UTC fallback rather than breaking the page.
    }
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
            const state = data.principalSubdivision || "";
            const country = data.countryName || "";
            locEl.textContent = [city, state, country].filter(Boolean).join(", ") || "location found";
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
    const MAX_ATTEMPTS = 55; // ~ 36s at 650ms/attempt -- above the search cascade's worst-case bound

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

    if (product.calories == null) {
      const warning = document.createElement("span");
      warning.className = "result-row__basis";
      warning.textContent = "No nutrition data on Open Food Facts for this listing";
      info.appendChild(warning);
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
    const MAX_ATTEMPTS = 55; // ~ 36s, same margin as pollSearch

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
    const modal = document.getElementById("entry-edit-modal");
    const closeBtn = document.getElementById("entry-edit-close");
    const nameEl = document.getElementById("entry-edit-name");
    const form = document.getElementById("entry-edit-form");
    const caloriesInput = document.getElementById("entry-edit-calories");
    const dateInput = document.getElementById("entry-edit-date");
    const timeInput = document.getElementById("entry-edit-time");
    const statusEl = document.getElementById("entry-edit-status");
    if (!modal || !form) return;

    let currentId = null;

    document.querySelectorAll("[data-adjust-log-id]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        currentId = btn.dataset.adjustLogId;
        nameEl.textContent = btn.dataset.adjustName || "";
        caloriesInput.value = btn.dataset.adjustCurrent || "";
        dateInput.value = btn.dataset.adjustDate || "";
        timeInput.value = btn.dataset.adjustTime || "";
        statusEl.textContent = "";
        modal.classList.add("is-open");
      });
    });

    form.addEventListener("submit", function (evt) {
      evt.preventDefault();
      if (!currentId) return;

      const payload = {};
      if (caloriesInput.value !== "") {
        const cal = parseFloat(caloriesInput.value);
        if (isNaN(cal) || cal < 0) {
          statusEl.textContent = "Enter a valid number of calories.";
          return;
        }
        payload.calories = cal;
      }
      if (dateInput.value) payload.date = dateInput.value;
      if (timeInput.value) payload.time = timeInput.value;

      statusEl.textContent = "Saving\u2026";
      fetch("/log/" + currentId + "/adjust", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      })
        .then(function (res) {
          if (!res.ok) return res.json().then(function (b) { throw new Error(b.error || "Couldn't update that."); });
          window.location.reload();
        })
        .catch(function (err) {
          statusEl.textContent = err.message;
        });
    });

    function close() { modal.classList.remove("is-open"); }
    if (closeBtn) closeBtn.addEventListener("click", close);
    modal.addEventListener("click", function (evt) {
      if (evt.target === modal) close();
    });
  }

  /* ----------------------------------------------------------------
     Generic helper: POST JSON to a URL, reload on success, show the
     server's error message (if any) in a status element on failure.
     Used by weigh-in, vacation, profile, and exercise forms below.
     ---------------------------------------------------------------- */

  function postJSON(url, payload, statusEl) {
    if (statusEl) setStatus(statusEl, "pending", "Saving\u2026");
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    })
      .then(function (res) {
        return res.json().then(function (body) {
          if (!res.ok) throw new Error(body.error || "Something went wrong.");
          return body;
        });
      })
      .then(function (body) {
        window.location.reload();
        return body;
      })
      .catch(function (err) {
        if (statusEl) setStatus(statusEl, "error", err.message);
        else window.alert(err.message);
      });
  }

  function postDelete(url) {
    fetch(url, { method: "POST" })
      .then(function (res) {
        if (!res.ok) throw new Error("delete failed");
        window.location.reload();
      })
      .catch(function () {
        window.alert("Couldn't remove that -- try again.");
      });
  }

  function initWeighInPage() {
    const form = document.getElementById("weigh-in-form");
    if (form) {
      form.addEventListener("submit", function (evt) {
        evt.preventDefault();
        const statusEl = document.getElementById("weigh-in-status");
        const weight = document.getElementById("weigh-in-weight").value;
        const date = document.getElementById("weigh-in-date").value;
        const notes = document.getElementById("weigh-in-notes").value.trim();
        postJSON("/weigh-in/add", { weight_lbs: weight, date: date, notes: notes }, statusEl);
      });
    }
    document.querySelectorAll("[data-delete-weigh-in-id]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        postDelete("/weigh-in/" + btn.dataset.deleteWeighInId + "/delete");
      });
    });
  }

  function initVacationPage() {
    const form = document.getElementById("vacation-form");
    if (form) {
      form.addEventListener("submit", function (evt) {
        evt.preventDefault();
        const statusEl = document.getElementById("vacation-status");
        postJSON("/vacation/add", {
          label: document.getElementById("vacation-label").value.trim(),
          start_date: document.getElementById("vacation-start").value,
          end_date: document.getElementById("vacation-end").value,
        }, statusEl);
      });
    }
    document.querySelectorAll("[data-delete-vacation-id]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        postDelete("/vacation/" + btn.dataset.deleteVacationId + "/delete");
      });
    });
  }

  function initDailyExtras() {
    const otdBody = document.getElementById("on-this-day-body");
    const newsBody = document.getElementById("patriots-news-body");
    if (!otdBody && !newsBody) return;

    function renderOnThisDay(events) {
      if (!otdBody) return;
      if (!events || !events.length) {
        otdBody.innerHTML = '<p class="empty-state">Couldn\u2019t load this today -- try again later.</p>';
        return;
      }
      const list = document.createElement("ul");
      list.className = "extras-list";
      events.forEach(function (e) {
        const li = document.createElement("li");
        li.className = "extras-list__item";
        const yearSpan = document.createElement("span");
        yearSpan.className = "extras-list__year";
        yearSpan.textContent = e.year;
        li.appendChild(yearSpan);
        const textEl = e.url ? document.createElement("a") : document.createElement("span");
        if (e.url) {
          textEl.href = e.url;
          textEl.target = "_blank";
          textEl.rel = "noopener";
        }
        textEl.className = "extras-list__text";
        textEl.textContent = e.text;
        li.appendChild(textEl);
        list.appendChild(li);
      });
      otdBody.innerHTML = "";
      otdBody.appendChild(list);
    }

    function renderPatriotsNews(items) {
      if (!newsBody) return;
      if (!items || !items.length) {
        newsBody.innerHTML = '<p class="empty-state">Couldn\u2019t load this today -- try again later.</p>';
        return;
      }
      const list = document.createElement("ul");
      list.className = "extras-list";
      items.forEach(function (n) {
        const li = document.createElement("li");
        li.className = "extras-list__item";
        const link = document.createElement("a");
        link.href = n.url;
        link.target = "_blank";
        link.rel = "noopener";
        link.className = "extras-list__text";
        link.textContent = n.title;
        li.appendChild(link);
        list.appendChild(li);
      });
      newsBody.innerHTML = "";
      newsBody.appendChild(list);
    }

    function poll(attempt) {
      fetch("/dashboard/daily-extras")
        .then(function (res) { return res.json(); })
        .then(function (data) {
          const otdReady = data.on_this_day && data.on_this_day.length;
          const newsReady = data.patriots_news && data.patriots_news.length;
          if (otdReady) renderOnThisDay(data.on_this_day);
          if (newsReady) renderPatriotsNews(data.patriots_news);

          // First-ever load: the background fetch may still be running.
          // Retry a few times, a few seconds apart, before giving up.
          if ((!otdReady || !newsReady) && attempt < 5) {
            setTimeout(function () { poll(attempt + 1); }, 3000);
          } else {
            if (!otdReady) renderOnThisDay(null);
            if (!newsReady) renderPatriotsNews(null);
          }
        })
        .catch(function () {
          renderOnThisDay(null);
          renderPatriotsNews(null);
        });
    }

    poll(0);
  }

  function initDashboardPage() {
    const profileForm = document.getElementById("profile-form");
    if (profileForm) {
      profileForm.addEventListener("submit", function (evt) {
        evt.preventDefault();
        const statusEl = document.getElementById("profile-status");
        postJSON("/dashboard/profile", {
          height_in: document.getElementById("profile-height").value,
          age: document.getElementById("profile-age").value,
          biological_sex: document.getElementById("profile-sex").value,
          activity_level: document.getElementById("profile-activity").value,
          goal_weight_lbs: document.getElementById("profile-goal-weight").value,
          program_start_date: document.getElementById("profile-start-date").value,
        }, statusEl);
      });
    }

    const exerciseForm = document.getElementById("exercise-form");
    if (exerciseForm) {
      exerciseForm.addEventListener("submit", function (evt) {
        evt.preventDefault();
        const statusEl = document.getElementById("exercise-status");
        const activity = document.getElementById("exercise-activity").value.trim();
        if (!activity) return;

        setStatus(statusEl, "pending", "Estimating based on your profile\u2026");

        fetch("/exercise/add", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ activity: activity }),
        })
          .then(function (res) {
            if (!res.ok) return res.json().then(function (b) { throw new Error(b.error || "Couldn't log that."); });
            return res.json();
          })
          .then(function (data) {
            pollExerciseEstimate(data.job_id, statusEl, 0);
          })
          .catch(function (err) {
            setStatus(statusEl, "error", err.message);
          });
      });
    }

    document.querySelectorAll("[data-delete-exercise-id]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        postDelete("/exercise/" + btn.dataset.deleteExerciseId + "/delete");
      });
    });

    document.querySelectorAll("[data-clear-day]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        const day = btn.dataset.clearDay;
        if (!window.confirm("Remove every exercise entry logged on " + day + "? This can't be undone.")) return;
        fetch("/exercise/delete-day", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ date: day }),
        })
          .then(function (res) {
            if (!res.ok) throw new Error("clear failed");
            window.location.reload();
          })
          .catch(function () {
            window.alert("Couldn't clear that day -- try again.");
          });
      });
    });
  }

  function pollExerciseEstimate(jobId, statusEl, attempt) {
    const MAX_ATTEMPTS = 55;

    fetch("/food/search/status/" + jobId)
      .then(function (res) { return res.json(); })
      .then(function (job) {
        if (job.status === "pending") {
          if (attempt >= MAX_ATTEMPTS) {
            setStatus(statusEl, "error", "This is taking longer than expected -- check back shortly.");
            return;
          }
          setTimeout(function () {
            pollExerciseEstimate(jobId, statusEl, attempt + 1);
          }, 700);
          return;
        }

        if (job.status === "error") {
          setStatus(statusEl, "error", job.error || "Couldn't log that.");
          return;
        }

        const cal = job.entry ? Math.round(job.entry.calories_burned) : null;
        setStatus(statusEl, "done", cal !== null ? ("Logged \u2248" + cal + " calories burned.") : "Logged.");
        setTimeout(function () { window.location.reload(); }, 1200);
      })
      .catch(function () {
        setStatus(statusEl, "error", "Lost track of that -- check back shortly.");
      });
  }

  /* ----------------------------------------------------------------
     AI food-logging agent: pick a meal type, describe what you had in
     plain language, Claude estimates nutrition and logs each distinct
     item directly. Also shows "recently logged for this meal" chips so
     repeating yesterday's breakfast is one tap, not retyping it.

     Parameterized by element IDs so the exact same logic drives both
     the inline Food Library form and the floating assistant modal
     (which exists on every page) without ID collisions between them.
     ---------------------------------------------------------------- */

  function loadAgentSuggestions(ids) {
    const mealType = document.getElementById(ids.mealType);
    const container = document.getElementById(ids.suggestions);
    if (!mealType || !container) return;

    fetch("/agent/recent-meals?meal_type=" + encodeURIComponent(mealType.value))
      .then(function (res) { return res.json(); })
      .then(function (data) {
        container.innerHTML = "";
        (data.suggestions || []).forEach(function (s) {
          const chip = document.createElement("div");
          chip.className = "suggestion-chip";

          const text = document.createElement("span");
          text.className = "suggestion-chip__text";
          text.textContent = s.summary;
          chip.appendChild(text);

          const cals = document.createElement("span");
          cals.className = "suggestion-chip__cals";
          cals.textContent = s.total_calories + " cal";
          chip.appendChild(cals);

          const btn = document.createElement("button");
          btn.type = "button";
          btn.textContent = "Log Again";
          btn.addEventListener("click", function () {
            btn.disabled = true;
            btn.textContent = "Logging\u2026";
            fetch("/agent/repeat", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ batch_id: s.batch_id }),
            })
              .then(function (res) {
                if (!res.ok) throw new Error("repeat failed");
                window.location.reload();
              })
              .catch(function () {
                btn.disabled = false;
                btn.textContent = "Log Again";
                window.alert("Couldn't repeat that -- try again.");
              });
          });
          chip.appendChild(btn);

          container.appendChild(chip);
        });
      })
      .catch(function () { /* suggestions are a nice-to-have -- fail quietly */ });
  }

  function maybePromptForPhotos(entries) {
    // Only food entries have a photos field at all (exercise entries
    // don't carry one) -- and only prompt for genuinely new items with
    // zero photos, not ones that already got a match from photo memory.
    const needsPhoto = (entries || []).filter(function (e) {
      return e && Array.isArray(e.photos) && e.photos.length === 0 && !e.photo_url && e.product_name;
    });

    if (!needsPhoto.length || !photoPickerOpenFor) {
      setTimeout(function () { window.location.reload(); }, 1400);
      return;
    }

    const queue = needsPhoto.slice();
    function openNext() {
      const next = queue.shift();
      if (!next) {
        window.location.reload();
        return;
      }
      photoPickerOpenFor(next.id, next.product_name, [], openNext);
    }
    openNext();
  }

  function pollAgentMessage(jobId, statusEl, attempt) {
    const MAX_ATTEMPTS = 55;

    fetch("/food/search/status/" + jobId)
      .then(function (res) { return res.json(); })
      .then(function (job) {
        if (job.status === "pending") {
          if (attempt >= MAX_ATTEMPTS) {
            setStatus(statusEl, "error", "This is taking longer than expected -- check your timeline shortly.");
            return;
          }
          setTimeout(function () {
            pollAgentMessage(jobId, statusEl, attempt + 1);
          }, 700);
          return;
        }

        if (job.status === "error") {
          setStatus(statusEl, "error", job.error || "Couldn't log that.");
          return;
        }

        setStatus(statusEl, "done", job.reply || "Logged.");
        setTimeout(function () { maybePromptForPhotos(job.entries); }, 900);
      })
      .catch(function () {
        setStatus(statusEl, "error", "Lost track of that -- check your timeline.");
      });
  }

  // Threaded variant for the floating chat: appends the reply as a
  // message bubble and keeps the modal open instead of reloading the
  // page after every message -- the person can keep the conversation
  // going. Whatever actually changed (items logged, adjusted, deleted)
  // is applied server-side immediately either way; only the *page's*
  // visible timeline/stats are deferred until the modal is closed.
  function pollAgentThreadMessage(jobId, pendingBubble, onSettled, attempt) {
    const MAX_ATTEMPTS = 55;

    fetch("/food/search/status/" + jobId)
      .then(function (res) { return res.json(); })
      .then(function (job) {
        if (job.status === "pending") {
          if (attempt >= MAX_ATTEMPTS) {
            pendingBubble.textContent = "This is taking longer than expected -- check your timeline shortly.";
            pendingBubble.classList.remove("agent-fab-msg--pending");
            pendingBubble.classList.add("agent-fab-msg--error");
            onSettled(false, null);
            return;
          }
          setTimeout(function () {
            pollAgentThreadMessage(jobId, pendingBubble, onSettled, attempt + 1);
          }, 700);
          return;
        }

        if (job.status === "error") {
          pendingBubble.textContent = job.error || "Couldn't do that.";
          pendingBubble.classList.remove("agent-fab-msg--pending");
          pendingBubble.classList.add("agent-fab-msg--error");
          onSettled(false, null);
          return;
        }

        const replyText = job.reply || "Done.";
        pendingBubble.textContent = replyText;
        pendingBubble.classList.remove("agent-fab-msg--pending");
        const madeChanges = (job.entries && job.entries.length) ||
          (job.adjusted && job.adjusted.length) ||
          (job.deleted && job.deleted.length);
        onSettled(!!madeChanges, replyText);
      })
      .catch(function () {
        pendingBubble.textContent = "Lost track of that -- check your timeline.";
        pendingBubble.classList.remove("agent-fab-msg--pending");
        pendingBubble.classList.add("agent-fab-msg--error");
        onSettled(false, null);
      });
  }

  function setupAgentForm(ids) {
    const form = document.getElementById(ids.form);
    if (!form) return;

    // mealType/suggestions are optional -- the inline "Tell The
    // Assistant" panel has both, the general-purpose floating chat has
    // neither (no pre-selected meal, so no per-meal suggestions to show).
    const mealType = ids.mealType ? document.getElementById(ids.mealType) : null;
    if (mealType && ids.suggestions) {
      mealType.addEventListener("change", function () { loadAgentSuggestions(ids); });
      loadAgentSuggestions(ids);
    }

    if (ids.thread) {
      // Persistent-conversation mode: the floating chat. Stays open,
      // appends bubbles, only reloads the page when it's closed (and
      // only if something was actually changed) -- handled by the
      // caller via ids.onSettled. Also keeps real conversation history
      // in memory and sends it with every message -- without this,
      // each turn was being answered with zero memory of what was just
      // discussed, which is its own bug distinct from the assistant
      // lacking profile/weigh-in data.
      const thread = document.getElementById(ids.thread);
      const history = [];

      form.addEventListener("submit", function (evt) {
        evt.preventDefault();
        const input = document.getElementById(ids.message);
        const message = input.value.trim();
        if (!message) return;

        const userBubble = document.createElement("div");
        userBubble.className = "agent-fab-msg agent-fab-msg--user";
        userBubble.textContent = message;
        thread.appendChild(userBubble);

        const pendingBubble = document.createElement("div");
        pendingBubble.className = "agent-fab-msg agent-fab-msg--assistant agent-fab-msg--pending";
        pendingBubble.textContent = "Thinking\u2026";
        thread.appendChild(pendingBubble);
        thread.scrollTop = thread.scrollHeight;

        input.value = "";
        input.disabled = true;

        fetch("/agent/message", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ message: message, meal_type: "", history: history }),
        })
          .then(function (res) {
            if (!res.ok) return res.json().then(function (b) { throw new Error(b.error || "Couldn't do that."); });
            return res.json();
          })
          .then(function (data) {
            pollAgentThreadMessage(data.job_id, pendingBubble, function (changed, replyText) {
              input.disabled = false;
              input.focus();
              thread.scrollTop = thread.scrollHeight;
              // Record this exchange for the next turn's memory --
              // only after it actually succeeded, so a failed request
              // doesn't pollute history with a garbage reply.
              if (replyText !== null) {
                history.push({ role: "user", content: message });
                history.push({ role: "assistant", content: replyText });
              }
              if (changed && ids.onSettled) ids.onSettled();
            }, 0);
          })
          .catch(function (err) {
            pendingBubble.textContent = err.message;
            pendingBubble.classList.remove("agent-fab-msg--pending");
            pendingBubble.classList.add("agent-fab-msg--error");
            input.disabled = false;
          });
      });
      return;
    }

    const statusEl = document.getElementById(ids.status);
    if (!statusEl) return;

    form.addEventListener("submit", function (evt) {
      evt.preventDefault();
      const input = document.getElementById(ids.message);
      const message = input.value.trim();
      if (!message) return;

      setStatus(statusEl, "pending", "Thinking\u2026");

      fetch("/agent/message", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: message, meal_type: mealType ? mealType.value : "" }),
      })
        .then(function (res) {
          if (!res.ok) return res.json().then(function (b) { throw new Error(b.error || "Couldn't log that."); });
          return res.json();
        })
        .then(function (data) {
          pollAgentMessage(data.job_id, statusEl, 0);
        })
        .catch(function (err) {
          setStatus(statusEl, "error", err.message);
        });
    });
  }

  /* ----------------------------------------------------------------
     Microphone dictation: browser-native Web Speech API, no external
     service or API call involved. Gracefully disables itself (rather
     than erroring) in browsers that don't support it.
     ---------------------------------------------------------------- */

  function initMicButton(micId, inputId) {
    const micBtn = document.getElementById(micId);
    const input = document.getElementById(inputId);
    if (!micBtn || !input) return;

    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SpeechRecognition) {
      micBtn.disabled = true;
      micBtn.style.opacity = "0.35";
      micBtn.title = "Dictation isn't supported in this browser";
      return;
    }

    const recognition = new SpeechRecognition();
    recognition.lang = navigator.language || "en-US";
    recognition.interimResults = false;
    recognition.maxAlternatives = 1;

    let listening = false;

    recognition.addEventListener("result", function (evt) {
      const transcript = evt.results[0][0].transcript;
      const existing = input.value.trim();
      input.value = existing ? existing + " " + transcript : transcript;
      input.focus();
    });

    recognition.addEventListener("end", function () {
      listening = false;
      micBtn.classList.remove("is-listening");
    });

    recognition.addEventListener("error", function () {
      listening = false;
      micBtn.classList.remove("is-listening");
    });

    micBtn.addEventListener("click", function () {
      if (listening) {
        recognition.stop();
        return;
      }
      listening = true;
      micBtn.classList.add("is-listening");
      try {
        recognition.start();
      } catch (e) {
        // Already-started or transient errors -- reset the visual state
        // rather than leaving the button stuck showing "listening".
        listening = false;
        micBtn.classList.remove("is-listening");
      }
    });
  }

  function initAgentForm() {
    setupAgentForm({
      form: "agent-form",
      mealType: "agent-meal-type",
      message: "agent-message",
      status: "agent-status",
      suggestions: "agent-suggestions",
    });
  }

  /* ----------------------------------------------------------------
     Photo picker: Plex-poster-picker style. Paste a URL, it's added to
     that entry's thumbnail row; several can be added in one session.
     No page reload while the modal's open -- only when it's closed, and
     only if something actually changed.
     ---------------------------------------------------------------- */

  /* ----------------------------------------------------------------
     Splash screen: tap anywhere to dismiss into the real app,
     remembered for the rest of this browser session via sessionStorage
     (not localStorage -- a fresh visit next session should see it
     again, this is a "welcome back" moment, not a permanent dismissal).
     ---------------------------------------------------------------- */

  function initSplashScreen() {
    const splash = document.getElementById("splash-screen");
    if (!splash) return;

    splash.addEventListener("click", function () {
      splash.style.transition = "opacity 0.25s ease";
      splash.style.opacity = "0";
      setTimeout(function () {
        splash.style.display = "none";
      }, 250);
    });
  }

  // Set by initPhotoPicker() once the modal exists on the current page
  // (Dashboard and Log both have it) -- lets other code open the picker
  // programmatically, e.g. auto-prompting for a photo right after
  // logging something new that doesn't have one yet.
  var photoPickerOpenFor = null;

  function initPhotoPicker() {
    const modal = document.getElementById("photo-picker-modal");
    const closeBtn = document.getElementById("photo-picker-close");
    const nameEl = document.getElementById("photo-picker-entry-name");
    const grid = document.getElementById("photo-picker-grid");
    const form = document.getElementById("photo-picker-form");
    const urlInput = document.getElementById("photo-picker-url");
    const statusEl = document.getElementById("photo-picker-status");
    if (!modal || !form || !grid) return;

    let currentEntryId = null;
    let changed = false;
    let onCloseCallback = null;

    function renderPhoto(photo) {
      const item = document.createElement("div");
      item.className = "photo-picker-item";
      item.dataset.photoId = photo.id;

      const img = document.createElement("img");
      img.src = photo.url;
      img.alt = "";
      img.loading = "lazy";
      item.appendChild(img);

      const removeBtn = document.createElement("button");
      removeBtn.type = "button";
      removeBtn.textContent = "\u00d7";
      removeBtn.setAttribute("aria-label", "Remove photo");
      removeBtn.addEventListener("click", function () {
        fetch("/log/" + currentEntryId + "/photos/" + photo.id + "/delete", { method: "POST" })
          .then(function (res) {
            if (!res.ok) throw new Error("remove failed");
            item.remove();
            changed = true;
          })
          .catch(function () {
            window.alert("Couldn't remove that photo -- try again.");
          });
      });
      item.appendChild(removeBtn);

      grid.appendChild(item);
    }

    function openFor(entryId, entryName, photos, onClose) {
      currentEntryId = entryId;
      nameEl.textContent = entryName || "";
      grid.innerHTML = "";
      statusEl.textContent = "";
      urlInput.value = "";
      onCloseCallback = onClose || null;

      (photos || []).forEach(renderPhoto);
      modal.classList.add("is-open");
      urlInput.focus();
    }
    photoPickerOpenFor = openFor;

    document.querySelectorAll("[data-open-photo-picker]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        let photos = [];
        try {
          photos = JSON.parse(btn.dataset.photos || "[]");
        } catch (e) { /* ignore malformed data, just show an empty grid */ }
        openFor(btn.dataset.openPhotoPicker, btn.dataset.entryName, photos, null);
      });
    });

    form.addEventListener("submit", function (evt) {
      evt.preventDefault();
      const url = urlInput.value.trim();
      if (!url || !currentEntryId) return;

      const submitBtn = form.querySelector("button[type=submit]");
      submitBtn.disabled = true;
      statusEl.textContent = "Adding\u2026";

      fetch("/log/" + currentEntryId + "/photos/add", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: url }),
      })
        .then(function (res) {
          return res.json().then(function (body) {
            if (!res.ok) throw new Error(body.error || "Couldn't add that photo.");
            return body;
          });
        })
        .then(function (photo) {
          renderPhoto(photo);
          urlInput.value = "";
          statusEl.textContent = "";
          changed = true;
        })
        .catch(function (err) {
          statusEl.textContent = err.message;
        })
        .finally(function () {
          submitBtn.disabled = false;
        });
    });

    function close() {
      modal.classList.remove("is-open");
      const cb = onCloseCallback;
      onCloseCallback = null;
      if (cb) {
        // A queued caller (e.g. the auto-prompt flow) handles its own
        // navigation/reload -- don't also do the default reload here.
        changed = false;
        cb();
        return;
      }
      if (changed) {
        window.location.reload();
      }
    }

    if (closeBtn) closeBtn.addEventListener("click", close);
    modal.addEventListener("click", function (evt) {
      if (evt.target === modal) close();
    });
  }

  function initAgentFab() {
    const fab = document.getElementById("agent-fab");
    const backdrop = document.getElementById("agent-fab-backdrop");
    const closeBtn = document.getElementById("agent-fab-close");
    if (!fab || !backdrop) return;

    let wired = false;
    let changed = false;

    fab.addEventListener("click", function () {
      backdrop.classList.add("is-open");
      if (!wired) {
        // Set up the modal's own form the first time it's opened, not on
        // every page load -- threaded mode: stays open across multiple
        // messages, only reloads the underlying page when closed.
        setupAgentForm({
          form: "fab-agent-form",
          message: "fab-agent-message",
          thread: "agent-fab-thread",
          onSettled: function () { changed = true; },
        });
        wired = true;
      }
    });

    function close() {
      backdrop.classList.remove("is-open");
      if (changed) {
        window.location.reload();
      }
    }

    if (closeBtn) closeBtn.addEventListener("click", close);
    backdrop.addEventListener("click", function (evt) {
      if (evt.target === backdrop) close();
    });
    document.addEventListener("keydown", function (evt) {
      if (evt.key === "Escape") close();
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    resolveTimezone();
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
    initWeighInPage();
    initVacationPage();
    initDashboardPage();
    initDailyExtras();
    initAgentForm();
    initAgentFab();
    initPhotoPicker();
    initSplashScreen();
    initMicButton("agent-mic", "agent-message");
    initMicButton("fab-agent-mic", "fab-agent-message");
  });
})();
