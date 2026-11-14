const $ = (s) => document.querySelector(s);
const DE = (n, d = 2) => (n ?? 0).toLocaleString("de-DE", { minimumFractionDigits: d, maximumFractionDigits: d });
const EUR = (n) => DE(n, 2) + " €";

const zustand = { einheit: "tag", metrik: "m3", anzahl: "" };
const BEZEICHNUNG = { stunde: "pro Stunde", tag: "pro Tag", monat: "pro Monat", jahr: "pro Jahr" };
const EINHEIT_KURZ = { stunde: "Stunde", tag: "Tag", monat: "Monat", jahr: "Jahr" };

// ------------------------------------------------------------------- Diagramm

function chartZeichnen(spalten, metrik) {
  const svg = $("#chart");
  const B = { x: 62, y: 18, breite: 1000 - 62 - 14, hoehe: 340 - 18 - 44 };
  const werte = spalten.map((s) => s[metrik] || 0);
  const max = Math.max(...werte, metrik === "m3" ? 0.05 : 0.5);
  const stufe = schöneStufe(max);
  const obergrenze = Math.ceil(max / stufe) * stufe;

  const einElement = (name, attr, kinder) => {
    const el = document.createElementNS("http://www.w3.org/2000/svg", name);
    for (const k in attr) el.setAttribute(k, attr[k]);
    (kinder || []).forEach((k) => el.appendChild(k));
    return el;
  };
  const text = (x, y, s, attr = {}) => {
    const el = einElement("text", { x, y, fill: "#8b98a9", "font-size": 11, ...attr });
    el.textContent = s;
    return el;
  };

  svg.replaceChildren();

  // Farbverlauf
  const defs = einElement("defs", {}, [
    einElement("linearGradient", { id: "balken", x1: 0, y1: 0, x2: 0, y2: 1 }, [
      einElement("stop", { offset: "0%", "stop-color": "#ffb066" }),
      einElement("stop", { offset: "100%", "stop-color": "#f0883e", "stop-opacity": ".55" }),
    ]),
  ]);
  svg.appendChild(defs);

  // Gitterlinien + y-Beschriftung
  const schritte = Math.round(obergrenze / stufe);
  for (let i = 0; i <= schritte; i++) {
    const wert = i * stufe;
    const y = B.y + B.hoehe - (wert / obergrenze) * B.hoehe;
    svg.appendChild(einElement("line", {
      x1: B.x, x2: B.x + B.breite, y1: y, y2: y,
      stroke: i === 0 ? "#33405a" : "#1e2836", "stroke-width": 1,
    }));
    svg.appendChild(text(B.x - 9, y + 4, achsenBeschriftung(wert, metrik), { "text-anchor": "end" }));
  }

  // Balken
  const n = spalten.length;
  const slot = B.breite / n;
  const balkenbreite = Math.max(2, Math.min(46, slot * 0.66));
  spalten.forEach((s, i) => {
    const wert = s[metrik] || 0;
    const h = (wert / obergrenze) * B.hoehe;
    const x = B.x + i * slot + (slot - balkenbreite) / 2;
    const y = B.y + B.hoehe - h;
    const balken = einElement("rect", {
      x, y: h > 2 ? y : B.y + B.hoehe - 2, width: balkenbreite, height: Math.max(h, 2),
      rx: Math.min(4, balkenbreite / 3), fill: "url(#balken)",
    });
    balken.style.cursor = "pointer";
    balken.addEventListener("mouseenter", (e) => tooltipZeigen(e, s, metrik));
    balken.addEventListener("mousemove", (e) => tooltipPosition(e));
    balken.addEventListener("mouseleave", () => $("#tooltip").classList.add("versteckt"));
    svg.appendChild(balken);

    if (wert > 0 && h > 22) {
      svg.appendChild(text(x + balkenbreite / 2, y - 5, wertBeschriftung(wert, metrik), {
        "text-anchor": "middle", fill: "#e6edf3", "font-size": 10.5,
      }));
    }
  });

  // x-Beschriftung (verdichten, wenn zu viele)
  const jedes = Math.ceil(n / 14);
  spalten.forEach((s, i) => {
    if (i % jedes) return;
    const x = B.x + i * slot + slot / 2;
    const el = text(x, B.y + B.hoehe + 20, kurzBeschriftung(s.periode, zustand.einheit),
      { "text-anchor": "middle" });
    svg.appendChild(el);
  });

  $("#diagrammTitel").textContent =
    `Verbrauch ${BEZEICHNUNG[zustand.einheit]} · ${metrik === "m3" ? "Kubikmeter" : metrik === "kwh" ? "Kilowattstunden" : "Kosten"}`;
  $("#legende").innerHTML =
    `<span><i style="background:#f0883e"></i>Verbrauch</span>` +
    `<span><i style="background:#58a6ff"></i>Summe Zeitraum: <b style="color:#58a6ff">${summenText(spalten)}</b></span>`;
}

function schöneStufe(max) {
  const zehner = Math.pow(10, Math.floor(Math.log10(max || 1)));
  for (const f of [1, 2, 2.5, 5, 10]) if (max / (zehner * f) <= 6) return zehner * f;
  return zehner * 10;
}
function achsenBeschriftung(w, metrik) {
  if (metrik === "eur") return DE(w, w < 10 ? 1 : 0) + "€";
  return DE(w, w < 1 ? 2 : w < 10 ? 1 : 0);
}
function wertBeschriftung(w, metrik) {
  if (metrik === "eur") return DE(w, w < 10 ? 1 : 0) + " €";
  if (metrik === "kwh") return DE(w, w < 10 ? 1 : 0);
  return DE(w, w < 1 ? 3 : w < 10 ? 2 : 1);
}
function kurzBeschriftung(periode, einheit) {
  if (einheit === "stunde") return periode.slice(11);        // "HH"
  if (einheit === "tag") return periode.slice(8, 10) + "." + periode.slice(5, 7);
  return periode;
}
function summenText(spalten) {
  const m3 = spalten.reduce((a, s) => a + s.m3, 0);
  const eur = spalten.reduce((a, s) => a + s.gesamt_eur, 0);
  return `${DE(m3, 2)} m³ · ${DE(gas(kwh(m3)), 0)} kWh · ${EUR(eur)}`;
}
let tarifCache = {};
const gas = (kwhWert) => kwhWert * tarifCache.arbeitspreis_ct_kwh / 100;
const kwh = (m3) => m3 * tarifCache.brennwert_kwh_m3 * tarifCache.zustandszahl;

function tooltipZeigen(eintrag, s, metrik) {
  const t = $("#tooltip");
  t.innerHTML =
    `<b>${periodeLang(s.periode)}</b><br>` +
    `Verbrauch: <b>${DE(s.m3, 3)} m³</b><br>` +
    `Energie: ${DE(s.kwh, 1)} kWh<br>` +
    `Kosten: <b>${EUR(s.eur)}</b> + Grundpreis ${EUR(s.gesamt_eur - s.eur)}<br>` +
    `gesamt: <b>${EUR(s.gesamt_eur)}</b>`;
  t.classList.remove("versteckt");
  tooltipPosition(eintrag);
}
function tooltipPosition(e) {
  const t = $("#tooltip");
  const container = t.parentElement.getBoundingClientRect();
  let x = e.clientX - container.left + 14;
  let y = e.clientY - container.top - 10;
  if (x + t.offsetWidth > container.width) x = e.clientX - container.left - t.offsetWidth - 14;
  t.style.left = Math.max(0, x) + "px";
  t.style.top = Math.max(0, y) + "px";
}
function periodeLang(p) {
  if (/^\d{4}-\d{2}-\d{2} \d{2}:00$/.test(p)) return "Stunde " + p.slice(11) + " Uhr, " + p.slice(8, 10) + "." + p.slice(5, 7);
  if (/^\d{4}-\d{2}-\d{2}$/.test(p)) return "Tag " + p.slice(8, 10) + "." + p.slice(5, 7) + "." + p.slice(0, 4);
  if (/^\d{4}-\d{2}$/.test(p)) return ["Jan", "Feb", "Mär", "Apr", "Mai", "Jun", "Jul", "Aug", "Sep", "Okt", "Nov", "Dez"][+p.slice(5) - 1] + " " + p.slice(0, 4);
  return "Jahr " + p;
}

// --------------------------------------------------------------------- Karten

function standAnzeige(wert) {
  const s = Number(wert).toFixed(1).padStart(7, "0");      // 8498.4 -> "08498.4"
  const [haupt, nach] = s.split(".");
  const kasten = (z, klasse) => `<span class="ziffer ${klasse}">${z}</span>`;
  return `<div class="ablesung">` +
    [...haupt].map((z) => kasten(z, "m3")).join("") +
    `<span class="komma">,</span>` +
    [...nach].map((z) => kasten(z, "dez")).join("") +
    `<span class="ziffer dez rollt">–</span>` +
    `<span class="ziffer dez rollt">–</span>` +
    `</div>`;
}

function zifferAnzeige(ablesung) {
  if (!ablesung || !/\d/.test(ablesung)) return "";
  const [haupt, nach] = ablesung.split(",");
  const kasten = (z, klasse) =>
    `<span class="ziffer ${klasse}${z === "-" ? " unbekannt" : ""}">${z}</span>`;
  return `<div class="ablesung">` +
    [...haupt].map((z) => kasten(z, "m3")).join("") +
    `<span class="komma">,</span>` +
    [...(nach || "")].map((z) => kasten(z, "dez")).join("") + `</div>`;
}

function kartenZeigen(k) {
  if (!k || !k.stand) { $("#karten").innerHTML = "<div class='karte'><div class='label'>Keine Daten</div><div class='wert'>–</div></div>"; return; }
  const karte = (label, wert, zusatz) =>
    `<div class="karte"><div class="label">${label}</div><div class="wert">${wert}</div><div class="zusatz">${zusatz}</div></div>`;
  $("#karten").innerHTML = [
    `<div class="karte"><div class="label">Zählerstand</div>` +
      standAnzeige(k.stand) +
      `<div class="zusatz">Auflösung 0,1 m³ · gemessen ${k.stand_text}</div></div>`,
    karte("Heute", DE(k.tag_m3, 2) + " m³", EUR(k.tag_eur) + " inkl. Grundpreis"),
    karte("Dieser Monat", DE(k.monat_m3, 2) + " m³", EUR(k.monat_eur)),
    karte("Dieses Jahr", DE(k.jahr_m3, 1) + " m³", EUR(k.jahr_eur)),
    karte("Ø pro Stunde (24 h)", DE(k.pro_stunde_m3, 3) + " m³", DE(k.pro_stunde_eur, 3) + " € / h"),
  ].join("");
  $("#standZeile").innerHTML =
    `Ablesung laut Bild: <b>${k.ablesung || "–"}</b> · ${k.messpunkte} Messpunkte`;
}

function prognoseZeigen(k) {
  if (!k || !k.stand) return;
  const p = (k2, m3, eur) =>
    `<div class="p"><div class="k">${k2}</div><div class="v">${DE(m3, m3 < 10 ? 2 : 1)} m³</div><div class="e">${EUR(eur)}</div></div>`;
  $("#prognose").innerHTML = [
    p("pro Stunde", k.pro_stunde_m3, k.pro_stunde_eur),
    p("pro Tag", k.pro_tag_m3, k.pro_tag_eur),
    p("pro Monat", k.pro_monat_m3, k.pro_monat_eur),
    p("pro Jahr", k.pro_jahr_m3, k.pro_jahr_eur),
  ].join("");
}

// ---------------------------------------------------------------- Tarif / UI

function tarifZeigen(t) {
  tarifCache = t;
  const badge = $("#tarifBadge");
  badge.textContent = `${t.anbieter} · ${t.tarifname} · ${DE(t.arbeitspreis_ct_kwh, 2)} ct/kWh`;
  badge.classList.toggle("offen", !t.bestaetigt);
  if ($("#tarifForm").dataset.aufgebaut !== "ja") {
    $("#tarifForm").dataset.aufgebaut = "ja";
    $("#tarifForm").innerHTML = feld("Anbieter", "anbieter") + feld("Tarifname", "tarifname", true)
      + feld("Arbeitspreis (ct/kWh)", "arbeitspreis_ct_kwh")
      + feld("Grundpreis (€/Monat)", "grundpreis_eur_monat")
      + feld("Brennwert (kWh/m³)", "brennwert_kwh_m3")
      + feld("Zustandszahl", "zustandszahl")
      + `<div class="ganz"><button type="submit" class="primar">Tarif speichern</button></div>`;
    $("#tarifForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const body = {};
      new FormData(e.target).forEach((v, k) => body[k] = v);
      const r = await fetch("/api/tarif", { method: "POST", body: JSON.stringify(body) });
      const j = await r.json();
      if (j.ok) { laden(); } else { alert(j.fehler); }
    });
  }
  for (const f of ["anbieter", "tarifname", "arbeitspreis_ct_kwh", "grundpreis_eur_monat", "brennwert_kwh_m3", "zustandszahl"]) {
    const el = $(`#tarifForm [name=${f}]`);
    if (el && document.activeElement !== el) el.value = t[f];
  }
}
const feld = (label, name, ganz) =>
  `<label${ganz ? ' class="ganz"' : ""}>${label}<input name="${name}" type="text"></label>`;

function detailZeigen(daten) {
  const meldungen = [];
  if (!daten.tarif.bestaetigt) {
    meldungen.push("⚠️ " + (daten.tarif.hinweis ||
      "Tarifwerte sind Platzhalter — bitte Werte aus der Vertragsbestätigung eintragen."));
  }
  if (!daten.basis_bestaetigt) {
    meldungen.push("⚠️ Bitte Zählerstand vom Display prüfen und unten mit „Zählerstand korrigieren“ bestätigen — nur dann sind die Werte absolut korrekt.");
  }
  if (daten.notiz) meldungen.push("ℹ️ " + daten.notiz);

  const warn = $("#warnung");
  if (meldungen.length) {
    warn.innerHTML = meldungen.join("<br>");
    warn.classList.remove("versteckt");
  } else {
    warn.classList.add("versteckt");
  }

  $("#fotos").innerHTML = daten.fotos.slice().reverse().map((f) => {
    const z = f.match(/(\d{8})-(\d{4})/);
    const beschr = z ? `${z[1].slice(6, 8)}.${z[1].slice(4, 6)}  ${z[2].slice(0, 2)}:${z[2].slice(2, 4)}` : f;
    return `<figure><img src="/fotos/${f}" loading="lazy" onclick="window.open('/fotos/${f}')">` +
      `<figcaption>${beschr}</figcaption></figure>`;
  }).join("") || "<p class='fuSS'>Noch keine Fotos.</p>";

  $("#messTabelle").innerHTML = daten.messungen.map((m) =>
    `<tr><td>${m.zeit}</td><td class="r">${DE(m.m3, 3)}</td>` +
    `<td class="mono">${m.ablesung || "–"}</td>` +
    `<td><span class="tag ${m.quelle}">${m.quelle}</span></td></tr>`).join("");

  const bank = daten.bank || {};
  const badge = $("#bankBadge");
  const erkannt = Object.keys(bank.labels || {}).length;
  badge.textContent = bank.vollstaendig
    ? `Lernbank vollständig (10/10 je Walze)`
    : `Lernbank: ${bank.anzahl || 0} Ziffern${erkannt ? `, ${erkannt}/8 Walzen bekannt` : " – Kalibrierung nötig"}`;
  badge.classList.toggle("offen", !bank.vollstaendig);

  const alt = daten.kennzahlen.stand_ts ? (Date.now() / 1000 - daten.kennzahlen.stand_ts) : null;
  $("#zuletzt").textContent = alt == null ? "noch keine Messung"
    : `letzte Messung vor ${Math.round(alt / 60)} min`;
  $("#fusion").textContent =
    `Erfassung: Cron alle 60 s · Zählerstand ${daten.kennzahlen.messpunkte || 0} Messpunkte · ` +
    `Zählerwerk-Tracking (Ziffernwechsel) · Kamera 192.168.188.43`;
}

// ------------------------------------------------------- Strom (Netzbezug)

const stromZustand = { einheit: "tag", metrik: "kwh", anzahl: "", art: "netz" };
const STROM_ARTEN = {
  netz: { titel: "Strombezug aus dem Netz", kurz: "Netzbezug", gesamt: "Netzbezug gesamt" },
  haus: { titel: "Hausverbrauch (Lastbezug)", kurz: "Hausverbrauch", gesamt: "Hausverbrauch gesamt" },
};
const STROM_BEZEICHNUNG = { stunde: "pro Stunde", tag: "pro Tag", monat: "pro Monat", jahr: "pro Jahr" };

function stromChartZeichnen(spalten, metrik) {
  const svg = $("#stromChart");
  if (!svg) return;
  const met = metrik === "eur" ? "eur" : "kwh";
  const B = { x: 62, y: 18, breite: 1000 - 62 - 14, hoehe: 340 - 18 - 44 };
  const werte = spalten.map((s) => s[met] || 0);
  const max = Math.max(...werte, met === "kwh" ? 0.5 : 0.05);
  const stufe = schöneStufe(max);
  const obergrenze = Math.ceil(max / stufe) * stufe;

  const el = (name, attr, kinder) => {
    const e = document.createElementNS("http://www.w3.org/2000/svg", name);
    for (const k in attr) e.setAttribute(k, attr[k]);
    (kinder || []).forEach((k) => e.appendChild(k));
    return e;
  };
  const txt = (x, y, s, attr = {}) => {
    const e = el("text", { x, y, fill: "#8b98a9", "font-size": 11, ...attr });
    e.textContent = s;
    return e;
  };

  svg.replaceChildren();
  svg.appendChild(el("defs", {}, [
    el("linearGradient", { id: "balkenStrom", x1: 0, y1: 0, x2: 0, y2: 1 }, [
      el("stop", { offset: "0%", "stop-color": "#79c0ff" }),
      el("stop", { offset: "100%", "stop-color": "#1f6feb", "stop-opacity": ".55" }),
    ]),
  ]));

  const schritte = Math.round(obergrenze / stufe);
  for (let i = 0; i <= schritte; i++) {
    const wert = i * stufe;
    const y = B.y + B.hoehe - (wert / obergrenze) * B.hoehe;
    svg.appendChild(el("line", {
      x1: B.x, x2: B.x + B.breite, y1: y, y2: y,
      stroke: i === 0 ? "#33405a" : "#1e2836", "stroke-width": 1,
    }));
    svg.appendChild(txt(B.x - 9, y + 4, achsenBeschriftung(wert, met), { "text-anchor": "end" }));
  }

  const n = spalten.length;
  const slot = B.breite / n;
  const bw = Math.max(2, Math.min(46, slot * 0.66));
  spalten.forEach((s, i) => {
    const wert = s[met] || 0;
    const h = (wert / obergrenze) * B.hoehe;
    const x = B.x + i * slot + (slot - bw) / 2;
    const y = B.y + B.hoehe - h;
    const balken = el("rect", {
      x, y: h > 2 ? y : B.y + B.hoehe - 2, width: bw, height: Math.max(h, 2),
      rx: Math.min(4, bw / 3), fill: "url(#balkenStrom)",
    });
    balken.style.cursor = "pointer";
    balken.addEventListener("mouseenter", (e) => stromTooltipZeigen(e, s));
    balken.addEventListener("mousemove", (e) => tooltipPosition(e));
    balken.addEventListener("mouseleave", () => $("#stromTooltip").classList.add("versteckt"));
    svg.appendChild(balken);
    if (wert > 0 && h > 22) {
      svg.appendChild(txt(x + bw / 2, y - 5, wertBeschriftung(wert, met), {
        "text-anchor": "middle", fill: "#e6edf3", "font-size": 10.5,
      }));
    }
  });

  const jedes = Math.ceil(n / 14);
  spalten.forEach((s, i) => {
    if (i % jedes) return;
    svg.appendChild(txt(B.x + i * slot + slot / 2, B.y + B.hoehe + 20,
      kurzBeschriftung(s.periode, stromZustand.einheit), { "text-anchor": "middle" }));
  });

  const beschriftung = STROM_ARTEN[stromZustand.art] || STROM_ARTEN.netz;
  const sumKwh = spalten.reduce((a, s) => a + s.kwh, 0);
  const sumEur = spalten.reduce((a, s) => a + s.gesamt_eur, 0);
  $("#stromTitel").textContent = `${beschriftung.titel} ${STROM_BEZEICHNUNG[stromZustand.einheit]} · `
    + (met === "kwh" ? "Kilowattstunden" : "Kosten");
  $("#stromLegende").innerHTML =
    `<span><i style="background:#1f6feb"></i>${beschriftung.kurz}</span>` +
    `<span><i style="background:#58a6ff"></i>Summe Zeitraum: ` +
    `<b style="color:#58a6ff">${DE(sumKwh, 2)} kWh · ${EUR(sumEur)}</b></span>`;
}

function stromTooltipZeigen(e, s) {
  const t = $("#stromTooltip");
  t.innerHTML =
    `<b>${periodeLang(s.periode)}</b><br>` +
    `${(STROM_ARTEN[stromZustand.art] || STROM_ARTEN.netz).kurz}: <b>${DE(s.kwh, 2)} kWh</b><br>` +
    `Arbeitspreis: <b>${EUR(s.eur)}</b><br>` +
    `+ Grundpreis: ${EUR(s.gesamt_eur - s.eur)}<br>` +
    `gesamt: <b>${EUR(s.gesamt_eur)}</b>`;
  t.classList.remove("versteckt");
  tooltipPosition(e);
}

function stromKartenZeigen(k, tarif) {
  const box = $("#stromKarten");
  if (!box) return;

  const stand = $("#stromStand");
  if (stand && tarif) {
    stand.textContent = `${tarif.anbieter || "Strom"} · ${DE(tarif.arbeitspreis_ct_kwh, 2)} ct/kWh`
      + (tarif.bestaetigt ? "" : " · Beispielwerte");
    stand.classList.toggle("offen", !tarif.bestaetigt);
  }

  if ($("#stromTarifForm") && $("#stromTarifForm").dataset.aufgebaut !== "ja") {
    $("#stromTarifForm").dataset.aufgebaut = "ja";
    $("#stromTarifForm").innerHTML = feld("Anbieter", "anbieter")
      + feld("Tarifname", "tarifname", true)
      + feld("Arbeitspreis (ct/kWh)", "arbeitspreis_ct_kwh")
      + feld("Grundpreis (€/Monat)", "grundpreis_eur_monat")
      + `<div class="ganz"><button type="submit" class="primar">Stromtarif speichern</button></div>`;
    $("#stromTarifForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const body = {};
      new FormData(e.target).forEach((v, k) => body[k] = v);
      const r = await fetch("/api/stromtarif", { method: "POST", body: JSON.stringify(body) });
      const j = await r.json();
      if (j.ok) { stromLaden(); } else { alert(j.fehler || "Fehler"); }
    });
  }
  if (tarif) {
    for (const f of ["anbieter", "tarifname", "arbeitspreis_ct_kwh", "grundpreis_eur_monat"]) {
      const el = $(`#stromTarifForm [name=${f}]`);
      if (el && document.activeElement !== el) el.value = tarif[f] ?? "";
    }
  }

  if (!k || k.stand_kwh == null) {
    box.innerHTML = "<div class='karte'><div class='label'>Noch keine Messung</div>"
      + "<div class='wert'>–</div><div class='zusatz'>Der Sammler schreibt alle 5 Minuten einen Wert.</div></div>";
    return;
  }
  const karte = (label, wert, zusatz) =>
    `<div class="karte"><div class="label">${label}</div><div class="wert">${wert}</div>`
    + `<div class="zusatz">${zusatz}</div></div>`;
  const alter = k.alter_sekunden == null ? "" :
    ` · vor ${Math.round(k.alter_sekunden / 60)} min`;
  box.innerHTML = [
    karte((STROM_ARTEN[stromZustand.art] || STROM_ARTEN.netz).gesamt, DE(k.stand_kwh, 1) + " kWh", `Zählerstand ${k.stand_text || "–"}${alter}`),
    karte("Heute", DE(k.tag_kwh, 2) + " kWh", EUR(k.tag_eur) + " Arbeitspreis"),
    karte("Dieser Monat", DE(k.monat_kwh, 1) + " kWh", EUR(k.monat_eur)),
    karte("Dieses Jahr", DE(k.jahr_kwh, 1) + " kWh", EUR(k.jahr_eur)),
  ].join("");
}

async function stromLaden() {
  const url = `/api/strom?einheit=${stromZustand.einheit}&art=${stromZustand.art}`
    + (stromZustand.anzahl ? `&anzahl=${stromZustand.anzahl}` : "");
  const antwort = await fetch(url);
  const d = await antwort.json();
  stromKartenZeigen(d.kennzahlen, d.tarif);
  stromChartZeichnen(d.spalten, stromZustand.metrik);
}


// ------------------------------------------------------------- Wasser

const wasserZustand = { einheit: "tag", metrik: "m3", anzahl: "" };
const WASSER_BEZEICHNUNG = { stunde: "pro Stunde", tag: "pro Tag", monat: "pro Monat", jahr: "pro Jahr" };

function wasserChartZeichnen(spalten, metrik) {
  const svg = $("#wasserChart");
  if (!svg) return;
  const met = metrik === "eur" ? "eur" : "m3";
  const B = { x: 62, y: 18, breite: 1000 - 62 - 14, hoehe: 340 - 18 - 44 };
  const werte = spalten.map((s) => s[met] || 0);
  const max = Math.max(...werte, met === "m3" ? 0.1 : 0.05);
  const stufe = schöneStufe(max);
  const obergrenze = Math.ceil(max / stufe) * stufe;

  const el = (name, attr, kinder) => {
    const e = document.createElementNS("http://www.w3.org/2000/svg", name);
    for (const k in attr) e.setAttribute(k, attr[k]);
    (kinder || []).forEach((k) => e.appendChild(k));
    return e;
  };
  const txt = (x, y, s, attr = {}) => {
    const e = el("text", { x, y, fill: "#8b98a9", "font-size": 11, ...attr });
    e.textContent = s;
    return e;
  };

  svg.replaceChildren();
  svg.appendChild(el("defs", {}, [
    el("linearGradient", { id: "balkenWasser", x1: 0, y1: 0, x2: 0, y2: 1 }, [
      el("stop", { offset: "0%", "stop-color": "#7ee2d0" }),
      el("stop", { offset: "100%", "stop-color": "#1f9e8a", "stop-opacity": ".55" }),
    ]),
  ]));

  const schritte = Math.round(obergrenze / stufe);
  for (let i = 0; i <= schritte; i++) {
    const wert = i * stufe;
    const y = B.y + B.hoehe - (wert / obergrenze) * B.hoehe;
    svg.appendChild(el("line", {
      x1: B.x, x2: B.x + B.breite, y1: y, y2: y,
      stroke: i === 0 ? "#33405a" : "#1e2836", "stroke-width": 1,
    }));
    svg.appendChild(txt(B.x - 9, y + 4,
      met === "eur" ? DE(wert, 2) + " €" : DE(wert, 2) + " m³",
      { "text-anchor": "end" }));
  }

  const n = spalten.length;
  const slot = B.breite / n;
  const bw = Math.max(2, Math.min(46, slot * 0.66));
  spalten.forEach((s, i) => {
    const wert = s[met] || 0;
    const h = (wert / obergrenze) * B.hoehe;
    const x = B.x + i * slot + (slot - bw) / 2;
    const y = B.y + B.hoehe - h;
    const balken = el("rect", {
      x, y: h > 2 ? y : B.y + B.hoehe - 2, width: bw, height: Math.max(h, 2),
      rx: Math.min(4, bw / 3), fill: "url(#balkenWasser)",
    });
    balken.style.cursor = "pointer";
    balken.addEventListener("mouseenter", (e) => wasserTooltipZeigen(e, s));
    balken.addEventListener("mousemove", (e) => tooltipPosition(e));
    balken.addEventListener("mouseleave", () => $("#wasserTooltip").classList.add("versteckt"));
    svg.appendChild(balken);
    if (wert > 0 && h > 22) {
      const beschriftung = met === "eur" ? DE(wert, 2) : DE(wert, 2);
      svg.appendChild(txt(x + bw / 2, y - 5, beschriftung, {
        "text-anchor": "middle", fill: "#e6edf3", "font-size": 10.5,
      }));
    }
  });

  const jedes = Math.ceil(n / 14);
  spalten.forEach((s, i) => {
    if (i % jedes) return;
    svg.appendChild(txt(B.x + i * slot + slot / 2, B.y + B.hoehe + 20,
      kurzBeschriftung(s.periode, wasserZustand.einheit), { "text-anchor": "middle" }));
  });

  const sumM3 = spalten.reduce((a, s) => a + s.m3, 0);
  const sumEur = spalten.reduce((a, s) => a + s.gesamt_eur, 0);
  $("#wasserTitel").textContent = `Wasserverbrauch ${WASSER_BEZEICHNUNG[wasserZustand.einheit]} · `
    + (met === "m3" ? "Kubikmeter" : "Kosten");
  $("#wasserLegende").innerHTML =
    `<span><i style="background:#1f9e8a"></i>Wasser</span>` +
    `<span><i style="background:#7ee2d0"></i>Summe Zeitraum: ` +
    `<b style="color:#7ee2d0">${DE(sumM3, 2)} m³ · ${EUR(sumEur)}</b></span>`;
}

function wasserTooltipZeigen(e, s) {
  const t = $("#wasserTooltip");
  t.innerHTML =
    `<b>${periodeLang(s.periode)}</b><br>` +
    `Wasser: <b>${DE(s.m3, 2)} m³</b><br>` +
    `Wasser/Abwasser: <b>${EUR(s.eur)}</b><br>` +
    `+ Grundpreis: ${EUR(s.gesamt_eur - s.eur)}<br>` +
    `gesamt: <b>${EUR(s.gesamt_eur)}</b>`;
  t.classList.remove("versteckt");
  tooltipPosition(e);
}

function wasserKartenZeigen(k, tarif) {
  const box = $("#wasserKarten");
  if (!box) return;

  const stand = $("#wasserStand");
  if (stand && tarif) {
    const preis = (Number(tarif.wasserpreis_eur_m3) || 0) + (Number(tarif.abwasser_eur_m3) || 0);
    stand.textContent = `${tarif.anbieter || "Wasser"} · ${DE(preis, 2)} €/m³`
      + (tarif.bestaetigt ? "" : " · Beispielwerte");
    stand.classList.toggle("offen", !tarif.bestaetigt);
  }

  if ($("#wasserTarifForm") && $("#wasserTarifForm").dataset.aufgebaut !== "ja") {
    $("#wasserTarifForm").dataset.aufgebaut = "ja";
    $("#wasserTarifForm").innerHTML = feld("Anbieter", "anbieter")
      + feld("Tarifname", "tarifname", true)
      + feld("Wasserpreis (€/m³)", "wasserpreis_eur_m3")
      + feld("Abwasser (€/m³)", "abwasser_eur_m3")
      + feld("Grundpreis (€/Monat)", "grundpreis_eur_monat")
      + `<div class="ganz"><button type="submit" class="primar">Wassertarif speichern</button></div>`;
    $("#wasserTarifForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const body = {};
      new FormData(e.target).forEach((v, k) => body[k] = v);
      const r = await fetch("/api/wassertarif", { method: "POST", body: JSON.stringify(body) });
      const j = await r.json();
      if (j.ok) { wasserLaden(); } else { alert(j.fehler || "Fehler"); }
    });
  }
  if (tarif) {
    for (const f of ["anbieter", "tarifname", "wasserpreis_eur_m3",
                     "abwasser_eur_m3", "grundpreis_eur_monat"]) {
      const el = $(`#wasserTarifForm [name=${f}]`);
      if (el && document.activeElement !== el) el.value = tarif[f] ?? "";
    }
  }

  if ($("#wasserKorrekturForm") && $("#wasserKorrekturForm").dataset.aufgebaut !== "ja") {
    $("#wasserKorrekturForm").dataset.aufgebaut = "ja";
    $("#wasserKorrekturForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const wert = $("#wasserKorrekturWert").value.trim();
      const r = await fetch("/api/wasserkorrektur", {
        method: "POST", body: JSON.stringify({ wert }),
      });
      const j = await r.json();
      const feldStatus = $("#wasserStatusZeile");
      if (j.ok) {
        feldStatus.textContent = `Zählerstand ${j.ablesung} m³ gesetzt.`;
        feldStatus.classList.remove("err");
        wasserLaden();
      } else {
        feldStatus.textContent = j.fehler || "Fehler";
        feldStatus.classList.add("err");
      }
    });
  }

  if (!k || k.stand_m3 == null) {
    box.innerHTML = "<div class='karte'><div class='label'>Noch keine Messung</div>"
      + "<div class='wert'>–</div><div class='zusatz'>Der Sammler liest alle 30 Minuten.</div></div>";
    return;
  }
  const karte = (label, wert, zusatz) =>
    `<div class="karte"><div class="label">${label}</div><div class="wert">${wert}</div>`
    + `<div class="zusatz">${zusatz}</div></div>`;
  const alter = k.alter_sekunden == null ? "" :
    ` · vor ${Math.round(k.alter_sekunden / 60)} min`;
  box.innerHTML = [
    karte("Zählerstand", (k.ablesung || "–") + " m³", `gemessen ${k.stand_text || "–"}${alter}`),
    karte("Heute", DE(k.tag_m3, 2) + " m³", EUR(k.tag_eur) + " inkl. Grundpreis"),
    karte("Dieser Monat", DE(k.monat_m3, 1) + " m³", EUR(k.monat_eur)),
    karte("Dieses Jahr", DE(k.jahr_m3, 1) + " m³", EUR(k.jahr_eur)),
  ].join("");
}

async function wasserLaden() {
  const url = `/api/wasser?einheit=${wasserZustand.einheit}`
    + (wasserZustand.anzahl ? `&anzahl=${wasserZustand.anzahl}` : "");
  const antwort = await fetch(url);
  const d = await antwort.json();
  wasserKartenZeigen(d.kennzahlen, d.tarif);
  wasserChartZeichnen(d.spalten, wasserZustand.metrik);
}

function wasserButtons() {
  const we = $("#wasserEinheit");
  if (!we) return;
  we.addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    wasserZustand.einheit = b.dataset.e; wasserZustand.anzahl = "";
    const wa = $("#wasserAnzahl"); if (wa) wa.value = "";
    we.querySelectorAll("button").forEach((x) => x.classList.toggle("aktiv", x === b));
    wasserLaden();
  });
  const wm = $("#wasserMetrik");
  wm.addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    wasserZustand.metrik = b.dataset.m;
    wm.querySelectorAll("button").forEach((x) => x.classList.toggle("aktiv", x === b));
    wasserLaden();
  });
  const wa = $("#wasserAnzahl");
  if (wa) wa.addEventListener("change", () => {
    wasserZustand.anzahl = wa.value; wasserLaden();
  });
}

// Live-Bild-Links auf den Hostnamen der Seite umstellen, damit sie auch
// vom Handy funktionieren (die Viewer laufen auf demselben Rechner).
document.querySelectorAll("[data-viewer]").forEach((a) => {
  a.href = `http://${location.hostname}:${a.dataset.viewer}/`;
});

// ------------------------------------------------------------------- Laden

async function laden() {
  const url = `/api/uebersicht?einheit=${zustand.einheit}` +
    (zustand.anzahl ? `&anzahl=${zustand.anzahl}` : "");
  const antwort = await fetch(url);
  const daten = await antwort.json();
  tarifZeigen(daten.tarif);
  kartenZeigen(daten.kennzahlen);
  prognoseZeigen(daten.kennzahlen);
  chartZeichnen(daten.spalten, zustand.metrik);
  stromLaden();
  wasserLaden();
  detailZeigen(daten);
}

function buttons() {
  $("#einheit").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    zustand.einheit = b.dataset.e; zustand.anzahl = "";
    $("#anzahl").value = "";
    [...e.currentTarget.children].forEach((x) => x.classList.toggle("aktiv", x === b));
    laden();
  });
  $("#metrik").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    zustand.metrik = b.dataset.m;
    [...e.currentTarget.children].forEach((x) => x.classList.toggle("aktiv", x === b));
    laden();
  });
  $("#anzahl").addEventListener("change", (e) => { zustand.anzahl = e.target.value; laden(); });
  $("#korrekturForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const wert = $("#korrekturWert").value;
    const r = await fetch("/api/korrektur", { method: "POST", body: JSON.stringify({ wert }) });
    const j = await r.json();
    const feld = $("#statusZeile");
    if (j.ok) {
      feld.textContent = `Zählerstand ${DE(j.wert, 3)} m³ gesetzt, Ziffernbank neu kalibriert (gelesen: ${j.ablesung}).`;
      feld.classList.remove("err"); laden();
    }
    else { feld.textContent = j.fehler || "Fehler"; feld.classList.add("err"); }
  });
}

function stromButtons() {
  const se = $("#stromEinheit");
  if (se) se.addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    stromZustand.einheit = b.dataset.e; stromZustand.anzahl = "";
    const sa = $("#stromAnzahl"); if (sa) sa.value = "";
    [...e.currentTarget.children].forEach((x) => x.classList.toggle("aktiv", x === b));
    stromLaden();
  });
  const sa_art = $("#stromArt");
  if (sa_art) sa_art.addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    stromZustand.art = b.dataset.a;
    [...e.currentTarget.children].forEach((x) => x.classList.toggle("aktiv", x === b));
    stromLaden();
  });
  const sm = $("#stromMetrik");
  if (sm) sm.addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    stromZustand.metrik = b.dataset.m;
    [...e.currentTarget.children].forEach((x) => x.classList.toggle("aktiv", x === b));
    stromLaden();
  });
  const sa = $("#stromAnzahl");
  if (sa) sa.addEventListener("change", (e) => { stromZustand.anzahl = e.target.value; stromLaden(); });
}

buttons();
stromButtons();
laden();
setInterval(laden, 30000);
