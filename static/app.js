const $ = (id) => document.getElementById(id);
let connectionVerified = false;
let lastResults = null;
let activeTab = "workbook";

function authPayload() {
  return {
    server_url: $("serverUrl").value.trim(),
    site_id: $("siteId").value.trim(),
    pat_name: $("patName").value.trim(),
    pat_secret: $("patSecret").value,
    api_version: $("apiVersion").value
  };
}
function showMessage(message, kind="info") {
  const el=$("globalMessage");
  el.className=`message ${kind}`;
  el.textContent=message;
  el.classList.remove("hidden");
  el.scrollIntoView({behavior:"smooth",block:"nearest"});
}
function clearMessage(){ $("globalMessage").classList.add("hidden"); }
function setBusy(button,busy,label) {
  button.disabled=busy;
  button.querySelector(".btn-label").textContent=busy?label:button.dataset.defaultLabel;
  button.querySelector(".btn-spinner").classList.toggle("hidden",!busy);
}
$("testConnection").dataset.defaultLabel="Verify connection";
$("runCompare").dataset.defaultLabel="Run comparison";

$("toggleSecret").addEventListener("click",()=>{
  const input=$("patSecret");
  const visible=input.type==="text";
  input.type=visible?"password":"text";
  $("toggleSecret").textContent=visible?"Show":"Hide";
});

$("authForm").addEventListener("submit",async(event)=>{
  event.preventDefault(); clearMessage();
  const button=$("testConnection");
  setBusy(button,true,"Verifying…");
  $("authStatus").textContent="Checking…";
  $("authStatus").className="step-status muted";
  try {
    const response=await fetch("/api/test-connection",{
      method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(authPayload())
    });
    const data=await response.json();
    if(!response.ok) throw new Error(data.detail||"Connection failed.");
    connectionVerified=true;
    $("authStatus").textContent="Connected";
    $("authStatus").className="step-status";
    $("compareStatus").textContent="Ready";
    $("compareStatus").className="step-status";
    $("comparePanel").classList.remove("locked");
    $("comparePanel").setAttribute("aria-disabled","false");
    ["sourceUrl","targetUrl","compareValues","tolerance","runCompare"].forEach(id=>$(id).disabled=false);
    showMessage("Tableau authentication successful. Enter both dashboard links below to continue. Credentials are held only in this page's memory and are not stored in browser storage.","success");
    $("sourceUrl").focus();
  } catch(error) {
    connectionVerified=false;
    $("authStatus").textContent="Not connected";
    $("authStatus").className="step-status muted";
    showMessage(error.message,"error");
  } finally { setBusy(button,false); }
});

$("compareForm").addEventListener("submit",async(event)=>{
  event.preventDefault(); clearMessage();
  if(!connectionVerified){showMessage("Verify your Tableau connection first.","error");return;}
  const button=$("runCompare");
  setBusy(button,true,"Comparing…");
  $("resultsSection").classList.add("hidden");
  try {
    const payload={
      ...authPayload(),
      source_url:$("sourceUrl").value.trim(),
      target_url:$("targetUrl").value.trim(),
      compare_values:$("compareValues").checked,
      numeric_tolerance:Number($("tolerance").value||0.001)
    };
    const response=await fetch("/api/compare",{
      method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)
    });
    const data=await response.json();
    if(!response.ok) throw new Error(data.detail||"Comparison failed.");
    lastResults=data;
    renderResults(data);
    showMessage("Comparison completed. Download the full report for CSV, Excel, JSON, logs, and other generated evidence.","success");
    $("resultsSection").classList.remove("hidden");
    $("resultsSection").scrollIntoView({behavior:"smooth",block:"start"});
  } catch(error) {
    showMessage(error.message,"error");
  } finally { setBusy(button,false); }
});

function asArray(value){return Array.isArray(value)?value:[];}
function countAll(data){return ["workbook_diffs","datasource_diffs","sheet_diffs","value_diffs"].reduce((n,k)=>n+asArray(data[k]).length,0);}
function renderResults(data){
  const summary=data.summary||{};
  const w=asArray(data.workbook_diffs), d=asArray(data.datasource_diffs), s=asArray(data.sheet_diffs), v=asArray(data.value_diffs);
  $("resultsSubtitle").textContent=`${data.source_workbook_name||"Original"} compared with ${data.target_workbook_name||"Updated"}`;
  $("totalDiffs").textContent=countAll(data);
  $("totalErrors").textContent=summary.total_errors??[...w,...d,...s,...v].filter(x=>String(x.severity||"").toUpperCase()==="ERROR").length;
  $("totalWarnings").textContent=summary.total_warnings??[...w,...d,...s,...v].filter(x=>String(x.severity||"").toUpperCase()==="WARNING").length;
  $("categoriesCount").textContent=[w,d,s,v].filter(x=>x.length>0).length;
  $("workbookCount").textContent=w.length;$("datasourceCount").textContent=d.length;$("sheetCount").textContent=s.length;$("valueCount").textContent=v.length;
  $("downloadReport").href=data.download_url;
  activeTab="workbook";
  document.querySelectorAll(".tab").forEach(t=>t.classList.toggle("active",t.dataset.tab===activeTab));
  renderTab();
}
const columnsByTab={
  workbook:[["field","Field"],["source_value","Original"],["target_value","Updated"],["severity","Severity"]],
  datasource:[["diff_type","Difference"],["datasource_name","Datasource"],["field","Field"],["source_value","Original"],["target_value","Updated"],["severity","Severity"]],
  sheet:[["diff_type","Difference"],["sheet_name","Sheet / dashboard"],["field","Field"],["source_value","Original"],["target_value","Updated"],["severity","Severity"]],
  value:[["view_name","View"],["field","Field"],["source_value","Original"],["target_value","Updated"],["severity","Severity"]]
};
const keyByTab={workbook:"workbook_diffs",datasource:"datasource_diffs",sheet:"sheet_diffs",value:"value_diffs"};
function esc(value){
  if(value===null||value===undefined||value==="")return "—";
  if(typeof value==="object")value=JSON.stringify(value);
  return String(value).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}
function renderTab(){
  if(!lastResults)return;
  const rows=asArray(lastResults[keyByTab[activeTab]]);
  const host=$("diffContent");
  if(!rows.length){
    host.innerHTML='<div class="empty-state"><div class="empty-icon">✓</div><strong>No differences reported in this category</strong><p>The comparison engine did not report any differences for this check.</p></div>';
    return;
  }
  const cols=columnsByTab[activeTab];
  host.innerHTML='<div class="diff-table-wrap"><table><thead><tr>'+cols.map(c=>`<th>${esc(c[1])}</th>`).join("")+'</tr></thead><tbody>'+rows.map(row=>'<tr>'+cols.map(([key])=>{
    const val=row[key];
    if(key==="severity"){
      const sev=String(val||"INFO").toLowerCase();
      return `<td><span class="severity ${["error","warning"].includes(sev)?sev:"info"}">${esc(val||"INFO")}</span></td>`;
    }
    return `<td>${key==="field"||key==="sheet_name"||key==="view_name"?`<strong>${esc(val)}</strong>`:esc(val)}</td>`;
  }).join("")+'</tr>').join("")+'</tbody></table></div>';
}
document.querySelectorAll(".tab").forEach(tab=>tab.addEventListener("click",()=>{
  activeTab=tab.dataset.tab;
  document.querySelectorAll(".tab").forEach(t=>t.classList.toggle("active",t===tab));
  renderTab();
}));

// Avoid leaving the PAT secret visible if the page is refreshed or navigated away from.
window.addEventListener("beforeunload",()=>{ $("patSecret").value=""; });
