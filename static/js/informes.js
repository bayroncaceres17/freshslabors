window.Informes = (function(){
  const qs = s => document.querySelector(s);

  function to_ddmmyyyy(val){ // de yyyy-mm-dd a dd/mm/yyyy
    if(!val) return "";
    const [y,m,d] = val.split("-");
    return `${d}/${m}/${y}`;
  }

  function buildUrl(base, q, dateISO, view){
    const dateParam = to_ddmmyyyy(dateISO);
    const params = new URLSearchParams();
    if(q) params.set("q", q);
    if(view) params.set("view", view);
    if(dateParam) params.set("date", dateParam);
    return `${base}?${params.toString()}`;
  }

  let state = { baseUrl:"", q:"", date:"", view:"tareas" };
  let debounceT;

  function refresh(){
    const url = buildUrl(state.baseUrl, state.q, state.date, state.view);
    fetch(url, {headers:{"X-Requested-With":"XMLHttpRequest"}})
      .then(r => r.text())
      .then(html => { qs("#cards").innerHTML = html; })
      .catch(console.error);
  }

  function bind(){
    const iq = qs("#q");
    const id = qs("#date");
    const iv = qs("#view");

    iq.addEventListener("input", () => {
      clearTimeout(debounceT);
      debounceT = setTimeout(() => { state.q = iq.value.trim(); refresh(); }, 250);
    });

    id.addEventListener("change", () => {
      state.date = id.value; // yyyy-mm-dd
      refresh();
    });

    iv.addEventListener("change", () => {
      state.view = iv.value;
      refresh();
    });
  }

  return {
    init(opts){
      state = Object.assign(state, opts || {});
      bind();
    }
  };
})();
