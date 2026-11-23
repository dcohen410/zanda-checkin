"use strict";

const $ = (id) => document.getElementById(id);

function showError(err) {
  const el = $("error");
  if (!err) { el.hidden = true; return; }
  el.textContent = String(err.message || err).replace(/^Error invoking remote method '[^']+': (Error: )?/, "");
  el.hidden = false;
}

async function showSites(orgName) {
  $("signin").hidden = true;
  $("sites").hidden = false;
  $("org").textContent = orgName;
  const list = $("site-list");
  list.textContent = "Loading sites…";
  try {
    const sites = await window.zc.listSites();
    list.textContent = "";
    if (!sites.length) list.textContent = "No sites yet. Add one in the admin portal.";
    for (const s of sites) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "site";
      b.disabled = !s.ready;
      const name = document.createElement("strong");
      name.textContent = s.name;
      const loc = document.createElement("small");
      loc.textContent = s.ready ? s.locationName : "Needs setup in the admin portal";
      b.append(name, loc);
      b.addEventListener("click", () => window.zc.chooseSite({ siteId: s.id, siteName: s.name }).catch(showError));
      list.append(b);
    }
  } catch (err) {
    list.textContent = "";
    showError(err);
  }
}

async function init() {
  const st = await window.zc.getState();
  $("status").textContent = st.status;
  $("server").value = st.server || "";
  $("email").value = st.email || "";
  if (st.signedIn) showSites(st.orgName);
  else $("signin").hidden = false;
}

$("signin").addEventListener("submit", async (e) => {
  e.preventDefault();
  showError(null);
  const btn = e.submitter;
  btn.disabled = true;
  try {
    const res = await window.zc.signIn({
      server: $("server").value, email: $("email").value, password: $("password").value,
      code: $("code").value,
    });
    $("password").value = "";
    $("code").value = "";
    showSites(res.orgName);
  } catch (err) {
    showError(err);
  } finally {
    btn.disabled = false;
  }
});

$("signout").addEventListener("click", async () => {
  await window.zc.signOut();
  $("sites").hidden = true;
  $("signin").hidden = false;
});

window.zc.onStatus((s) => { $("status").textContent = s; });
init();
