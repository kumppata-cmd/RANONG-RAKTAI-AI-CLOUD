/* HD voice note → flowsheet fields (ว้าว #4).
   Pure parser + thin wrapper over the browser's SpeechRecognition (th-TH).
   Nothing is saved automatically: it only fills the entry form, the nurse
   reviews and presses บันทึก. Works offline-free (no server, no external AI). */
(function (root) {
  'use strict';

  var NUM = '(-?\\d+(?:[.,]\\d+)?)';
  function num(s) { return s == null ? null : parseFloat(String(s).replace(',', '.')); }

  /* field → list of regexes (first match wins). Thai STT usually emits digits. */
  var RULES = [
    ['bp',     ['(?:bp|บีพี|ความดัน(?:โลหิต)?|blood pressure)\\s*' + NUM + '\\s*(?:/|ทับ|\\s)\\s*' + NUM,
                '\\b' + NUM + '\\s*(?:/|ทับ)\\s*' + NUM]],
    ['pr',     ['(?:pr|ชีพจร|pulse|หัวใจ|พีอาร์)\\s*' + NUM]],
    ['rr',     ['(?:rr|หายใจ|resp(?:iration)?|อาร์อาร์)\\s*' + NUM]],
    ['spo2',   ['(?:spo2|สปอ|ออกซิเจน|sat)\\s*' + NUM]],
    ['temp',   ['(?:อุณหภูมิ|temp(?:erature)?|ไข้)\\s*' + NUM]],
    ['bfr',    ['(?:bfr|บีเอฟอาร์|blood flow|เลือด|โบลดโฟลว์)\\s*' + NUM]],
    ['vp',     ['(?:vp|วีพี|venous)\\s*' + NUM]],
    ['ap',     ['(?:ap|เอพี|arterial)\\s*(?:ลบ|minus|-)?\\s*' + NUM]],
    ['tmp',    ['(?:tmp|ทีเอ็มพี)\\s*' + NUM]],
    ['conduct',['(?:conduct(?:ivity)?|คอนดัก)\\s*' + NUM]],
    ['dtemp',  ['(?:dialysate temp|น้ำยา|ไดอะไลเสท)\\s*' + NUM]],
    ['ufr',    ['(?:ufr|ยูเอฟอาร์|uf rate)\\s*' + NUM]],
    ['total_uf',['(?:total uf|ยูเอฟรวม|uf รวม|ดึงน้ำ(?:ได้|รวม)?|uf|ยูเอฟ)\\s*' + NUM]]
  ];

  function parseVoice(text) {
    var out = {}, used = [];
    var t = ' ' + String(text || '').replace(/\s+/g, ' ') + ' ';
    RULES.forEach(function (rule) {
      var key = rule[0], pats = rule[1];
      for (var i = 0; i < pats.length; i++) {
        var re = new RegExp(pats[i], 'i'), m = re.exec(t);
        if (!m) continue;
        if (key === 'bp') { out.sbp = num(m[1]); out.dbp = num(m[2]); }
        else if (key === 'ap') { var v = num(m[1]); out.ap = v > 0 && /ลบ|minus|-\s*\d/.test(m[0]) ? -v : v; }
        else out[key] = num(m[1]);
        used.push(m[0].trim());
        t = t.replace(m[0], ' ');
        break;
      }
    });
    var rm = /(?:หมายเหตุ|remark|โน้ต|note)\s*[:：]?\s*(.+)$/i.exec(t.trim());
    if (rm && rm[1].trim()) out.remark = rm[1].trim();
    return { fields: out, used: used, rest: t.replace(/\s+/g, ' ').trim() };
  }

  function supported() {
    return !!(root.SpeechRecognition || root.webkitSpeechRecognition);
  }

  /* start(onResult(transcript, isFinal), onEnd(err)) → recognizer or null */
  function start(onResult, onEnd) {
    var SR = root.SpeechRecognition || root.webkitSpeechRecognition;
    if (!SR) { onEnd && onEnd('unsupported'); return null; }
    var r = new SR();
    r.lang = 'th-TH'; r.interimResults = true; r.continuous = false; r.maxAlternatives = 1;
    r.onresult = function (e) {
      var txt = '', fin = false;
      for (var i = e.resultIndex; i < e.results.length; i++) { txt += e.results[i][0].transcript; if (e.results[i].isFinal) fin = true; }
      onResult && onResult(txt, fin);
    };
    r.onerror = function (e) { onEnd && onEnd(e.error || 'error'); };
    r.onend = function () { onEnd && onEnd(null); };
    try { r.start(); } catch (e) { onEnd && onEnd('start-failed'); return null; }
    return r;
  }

  var api = { parseVoice: parseVoice, supported: supported, start: start };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  root.HDVoice = api;
})(typeof window !== 'undefined' ? window : this);
