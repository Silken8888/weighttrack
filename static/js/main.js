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
        setTimeout(function () { window.location.reload(); }, 1400);
      })
      .catch(function () {
        setStatus(statusEl, "error", "Lost track of that -- check your timeline.");
      });
  }

  function setupAgentForm(ids) {
    const form = document.getElementById(ids.form);
    const statusEl = document.getElementById(ids.status);
    if (!form || !statusEl) return;

    // mealType/suggestions are optional -- the inline "Tell The
    // Assistant" panel has both, the general-purpose floating chat has
    // neither (no pre-selected meal, so no per-meal suggestions to show).
    const mealType = ids.mealType ? document.getElementById(ids.mealType) : null;
    if (mealType && ids.suggestions) {
      mealType.addEventListener("change", function () { loadAgentSuggestions(ids); });
      loadAgentSuggestions(ids);
    }

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

    document.querySelectorAll("[data-open-photo-picker]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        currentEntryId = btn.dataset.openPhotoPicker;
        nameEl.textContent = btn.dataset.entryName || "";
        grid.innerHTML = "";
        statusEl.textContent = "";
        urlInput.value = "";

        let photos = [];
        try {
          photos = JSON.parse(btn.dataset.photos || "[]");
        } catch (e) { /* ignore malformed data, just show an empty grid */ }
        photos.forEach(renderPhoto);

        modal.classList.add("is-open");
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

    fab.addEventListener("click", function () {
      backdrop.classList.add("is-open");
      if (!wired) {
        // Set up the modal's own form the first time it's opened, not on
        // every page load -- it's identical logic to the page form, just
        // pointed at the fab-prefixed element IDs.
        setupAgentForm({
          form: "fab-agent-form",
          message: "fab-agent-message",
          status: "fab-agent-status",
        });
        wired = true;
      }
    });

    function close() {
      backdrop.classList.remove("is-open");
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
    initAgentForm();
    initAgentFab();
    initPhotoPicker();
  });
})();
