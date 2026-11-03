// Веб-кабинет Sobesnik (009 §7, §8): счётчик у долгих операций и запись голоса.
// Без сборки и внешних библиотек, кроме htmx из этой же папки.
"use strict";

// --- долгие операции: кнопка блокируется, рядом «Разбираю вакансию… 12 с» ----------------

const timers = new WeakMap();

document.addEventListener("htmx:beforeRequest", (event) => {
  const flash = document.getElementById("flash");
  if (flash) flash.innerHTML = "";
  const form = event.detail.elt.closest("form[data-wait]");
  if (!form) return;
  form.querySelectorAll("button").forEach((b) => (b.disabled = true));
  let note = form.querySelector(".waiting");
  if (!note) {
    note = document.createElement("p");
    note.className = "waiting";
    note.setAttribute("aria-live", "polite");
    form.appendChild(note);
  }
  const started = Date.now();
  const tick = () => {
    note.textContent = `${form.dataset.wait} · ${Math.round((Date.now() - started) / 1000)} с`;
  };
  tick();
  timers.set(form, setInterval(tick, 1000));
});

document.addEventListener("htmx:afterRequest", (event) => {
  const form = event.detail.elt.closest("form[data-wait]");
  if (!form) return;
  clearInterval(timers.get(form));
  form.querySelectorAll("button").forEach((b) => (b.disabled = false));
  const note = form.querySelector(".waiting");
  if (note && !event.detail.xhr.getResponseHeader("HX-Redirect")) note.remove();
});

document.addEventListener("htmx:sendError", () => {
  showFlash("Нет связи с веб-кабинетом. Проверьте, что Sobesnik запущен: docker compose ps.");
});

function showFlash(text) {
  const flash = document.getElementById("flash");
  if (!flash) return;
  const p = document.createElement("p");
  p.className = "error";
  p.textContent = text;
  flash.replaceChildren(p);
}

// --- голос: MediaRecorder → прослушать → «Отправить» или «Записать заново» ----------------

const MAX_SECONDS = 300; // ANS-2: до 5 минут
const MIME_TYPES = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"];

function pickMime() {
  if (typeof MediaRecorder === "undefined") return null;
  return MIME_TYPES.find((t) => MediaRecorder.isTypeSupported(t)) || "";
}

function extensionFor(mime) {
  if (mime.startsWith("audio/mp4")) return "m4a";
  if (mime.startsWith("audio/ogg")) return "ogg";
  return "webm";
}

async function sendAudio(box, blob, filename) {
  const form = new FormData();
  form.append("position", box.dataset.position);
  form.append("total", box.dataset.total);
  form.append("file", blob, filename);
  box.querySelectorAll("button").forEach((b) => (b.disabled = true));
  setNote(box, "");
  let response;
  try {
    response = await fetch(box.dataset.url, {
      method: "POST",
      body: form,
      headers: { "HX-Request": "true" },
    });
  } catch {
    box.querySelectorAll("button").forEach((b) => (b.disabled = false));
    setNote(box, "Нет связи с веб-кабинетом. Запись не отправлена — попробуйте ещё раз.");
    return;
  }
  const redirect = response.headers.get("HX-Redirect");
  if (redirect) {
    window.location.href = redirect;
    return;
  }
  const html = await response.text();
  if (response.headers.get("HX-Retarget") === "#flash") {
    box.querySelectorAll("button").forEach((b) => (b.disabled = false));
    document.getElementById("flash").innerHTML = html;
    return;
  }
  const area = document.getElementById("answer-area");
  area.innerHTML = html;
  htmx.process(area);
  setupAll(area); // упавший ответ возвращает форму записи заново
}

function setNote(box, text) {
  const note = box.querySelector("[data-note]");
  note.textContent = text;
  note.hidden = !text;
}

function setupRecorder(box) {
  if (box.dataset.ready) return;
  box.dataset.ready = "1";
  const start = box.querySelector('[data-act="start"]');
  const stop = box.querySelector('[data-act="stop"]');
  const timer = box.querySelector("[data-timer]");
  const review = box.querySelector("[data-review]");
  const audio = review.querySelector("audio");
  let recorder = null;
  let chunks = [];
  let blob = null;
  let ticker = null;
  let started = 0;

  const mime = pickMime();
  if (mime === null || !navigator.mediaDevices) {
    start.disabled = true;
    setNote(box, "Браузер не умеет записывать звук здесь. Загрузите файл или ответьте текстом.");
  }

  const stopTicker = () => {
    clearInterval(ticker);
    ticker = null;
  };

  start.addEventListener("click", async () => {
    setNote(box, "");
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch {
      setNote(box, "Нет доступа к микрофону. Разрешите его в настройках браузера или загрузите файл.");
      return;
    }
    chunks = [];
    recorder = mime ? new MediaRecorder(stream, { mimeType: mime }) : new MediaRecorder(stream);
    recorder.addEventListener("dataavailable", (e) => e.data.size && chunks.push(e.data));
    recorder.addEventListener("stop", () => {
      stream.getTracks().forEach((t) => t.stop());
      stopTicker();
      blob = new Blob(chunks, { type: recorder.mimeType || mime || "audio/webm" });
      if (audio.src) URL.revokeObjectURL(audio.src);
      audio.src = URL.createObjectURL(blob);
      review.hidden = false;
      stop.hidden = true;
      start.hidden = true;
    });
    recorder.start();
    started = Date.now();
    start.hidden = true;
    stop.hidden = false;
    review.hidden = true;
    const tick = () => {
      const s = Math.round((Date.now() - started) / 1000);
      timer.textContent = `● ${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")} из 5:00`;
      if (s >= MAX_SECONDS && recorder.state === "recording") recorder.stop();
    };
    tick();
    ticker = setInterval(tick, 500);
  });

  stop.addEventListener("click", () => recorder && recorder.state === "recording" && recorder.stop());

  box.querySelector('[data-act="again"]').addEventListener("click", () => {
    blob = null;
    review.hidden = true;
    timer.textContent = "";
    start.hidden = false;
    start.click();
  });

  box.querySelector('[data-act="send"]').addEventListener("click", () => {
    if (blob) sendAudio(box, blob, `voice.${extensionFor(blob.type)}`);
  });

  box.querySelector('[data-act="upload"]').addEventListener("click", () => {
    const file = box.querySelector("[data-file]").files[0];
    if (!file) {
      setNote(box, "Выберите файл с записью.");
      return;
    }
    sendAudio(box, file, file.name);
  });
}

function setupAll(root) {
  root.querySelectorAll("[data-recorder]").forEach(setupRecorder);
}

document.addEventListener("DOMContentLoaded", () => setupAll(document));
document.addEventListener("htmx:load", (event) => setupAll(event.detail.elt));
