(function(){
"use strict";
var S={nodes:[],edges:[],selected:null,selectedEdge:null,nextId:1,facets:null,results:null,chartInstance:null};

var NODE_META={
  vacancies_source:{label:"Vacancies",cat:"source",color:"#10b981"},
  companies_source:{label:"Companies",cat:"source",color:"#10b981"},
  filter_role:{label:"Filter by Role",cat:"filter",color:"#3b82f6"},
  filter_domain:{label:"Filter by Domain",cat:"filter",color:"#3b82f6"},
  filter_seniority:{label:"Filter by Seniority",cat:"filter",color:"#3b82f6"},
  filter_salary:{label:"Filter by Salary",cat:"filter",color:"#3b82f6"},
  filter_location:{label:"Filter by Location",cat:"filter",color:"#3b82f6"},
  filter_employment:{label:"Filter by Employment",cat:"filter",color:"#3b82f6"},
  filter_source:{label:"Filter by Source",cat:"filter",color:"#3b82f6"},
  filter_skill:{label:"Filter by Skill",cat:"filter",color:"#3b82f6"},
  filter_risk:{label:"Filter by Risk",cat:"filter",color:"#3b82f6"},
  filter_score:{label:"Filter by Score",cat:"filter",color:"#3b82f6"},
  filter_company:{label:"Filter by Company",cat:"filter",color:"#3b82f6"},
  filter_date_range:{label:"Date Range",cat:"filter",color:"#3b82f6"},
  group_by:{label:"Group By",cat:"agg",color:"#8b5cf6"},
  aggregate:{label:"Aggregate",cat:"agg",color:"#8b5cf6"},
  sort:{label:"Sort",cat:"agg",color:"#8b5cf6"},
  limit:{label:"Limit",cat:"agg",color:"#8b5cf6"},
  chart:{label:"Chart",cat:"output",color:"#f59e0b"},
  table_view:{label:"Table View",cat:"output",color:"#f59e0b"},
  export_csv:{label:"Export CSV",cat:"output",color:"#f59e0b"},
};

var NODE_DESC={
  vacancies_source:"Loads all vacancies from the database. This is typically the starting point of your pipeline.",
  companies_source:"Loads all companies from the database.",
  filter_role:"Keep only vacancies with selected roles (e.g. Backend, Frontend, DevOps).",
  filter_domain:"Filter by domain (e.g. FinTech, E-Commerce, Healthcare).",
  filter_seniority:"Filter by level (Junior, Middle, Senior, Lead, etc.).",
  filter_salary:"Filter by salary range in USD. Enable 'Require salary' to skip vacancies without salary.",
  filter_location:"Filter by work location (Remote, Office, Hybrid).",
  filter_employment:"Filter by employment type (Full-time, Part-time, Contract).",
  filter_source:"Filter by data source (Telegram channel, web scraper, etc.).",
  filter_skill:"Filter by required skills/technologies.",
  filter_risk:"Filter by vacancy risk level.",
  filter_score:"Filter by AI quality score (0-10).",
  filter_company:"Keep only vacancies from selected companies.",
  filter_date_range:"Limit results to a time window (e.g. last 30 days).",
  group_by:"Group rows by a field. Required before Aggregate. Example: group by Role to see counts per role.",
  aggregate:"Calculate metrics: count, avg, sum, min, max. Add multiple functions. Aliases become column names for Sort.",
  sort:"Order results by a column. The dropdown shows fields available from Group By + Aggregate nodes upstream.",
  limit:"Limit the number of rows returned.",
  chart:"Visualize results as a chart. First column = labels, other columns = values.",
  table_view:"Display results as a table.",
  export_csv:"Download results as a CSV file.",
};

var VACANCY_FIELDS=[
  {v:"company_name",l:"Company"},{v:"role",l:"Role"},{v:"seniority",l:"Seniority"},
  {v:"domain",l:"Domain"},{v:"location_type",l:"Location Type"},{v:"risk_label",l:"Risk Label"},
  {v:"english_level",l:"English Level"},{v:"country_city",l:"Country/City"},
  {v:"source_channel",l:"Source"},{v:"category",l:"Category"},{v:"standardized_title",l:"Title"},
  {v:"created_at",l:"Date"},
];
var AGG_FIELDS=[
  {v:"id",l:"Count (id)"},{v:"salary_min_usd",l:"Salary Min"},{v:"salary_max_usd",l:"Salary Max"},
  {v:"ai_score_value",l:"AI Score"},{v:"experience_years",l:"Experience"},
];
var AGG_FNS=["count","avg","sum","min","max"];
var DATE_TRUNCS=[{v:"day",l:"Day"},{v:"week",l:"Week"},{v:"month",l:"Month"},{v:"quarter",l:"Quarter"},{v:"year",l:"Year"}];

async function init(){
  try{S.facets=await(await fetch("/api/analytics/facets")).json();}catch(e){S.facets={};}
  setupDragDrop();
}
init();

function setupDragDrop(){
  document.querySelectorAll(".palette-node").forEach(function(el){
    el.addEventListener("dragstart",function(e){e.dataTransfer.setData("node_type",el.dataset.type);e.dataTransfer.effectAllowed="copy";});
  });
  var area=document.getElementById("canvasArea");
  area.addEventListener("dragover",function(e){e.preventDefault();e.dataTransfer.dropEffect="copy";});
  area.addEventListener("drop",function(e){
    e.preventDefault();
    var type=e.dataTransfer.getData("node_type");
    if(!type||!NODE_META[type])return;
    var rect=document.getElementById("canvasInner").getBoundingClientRect();
    var dx=e.clientX-rect.left, dy=e.clientY-rect.top;
    var hitEdge=findEdgeNearPoint(dx,dy);
    if(hitEdge!==null){
      insertNodeOnEdge(type,dx,dy,hitEdge);
    } else {
      addNode(type,dx,dy);
    }
  });
  area.addEventListener("mousedown",function(e){
    if(!e.target.closest(".c-node")&&!e.target.closest(".port")&&!e.target.closest(".hit-area")){
      S.selected=null;S.selectedEdge=null;
      closeConfig();renderCanvas();
    }
  });
  document.addEventListener("keydown",function(e){
    if(e.target.tagName==="INPUT"||e.target.tagName==="SELECT"||e.target.tagName==="TEXTAREA")return;
    if(e.key==="Backspace"||e.key==="Delete"){
      e.preventDefault();
      if(S.selected){removeNode(S.selected);}
      else if(S.selectedEdge!==null){removeEdge(S.selectedEdge);}
    }
    if(e.key==="Escape"){
      S.selected=null;S.selectedEdge=null;closeConfig();renderCanvas();
    }
  });
}

function findEdgeNearPoint(px,py){
  var threshold=14;
  for(var i=0;i<S.edges.length;i++){
    var edge=S.edges[i];
    var fromEl=document.getElementById("cnode-"+edge.from);
    var toEl=document.getElementById("cnode-"+edge.to);
    if(!fromEl||!toEl)continue;
    var x1=fromEl.offsetLeft+fromEl.offsetWidth,y1=fromEl.offsetTop+fromEl.offsetHeight/2;
    var x2=toEl.offsetLeft,y2=toEl.offsetTop+toEl.offsetHeight/2;
    if(distToBezier(px,py,x1,y1,x2,y2)<threshold)return i;
  }
  return null;
}

function distToBezier(px,py,x1,y1,x2,y2){
  var dx=Math.abs(x2-x1)*0.5;
  var cx1=x1+dx,cy1=y1,cx2=x2-dx,cy2=y2;
  var minD=Infinity;
  for(var t=0;t<=1;t+=0.05){
    var u=1-t;
    var bx=u*u*u*x1+3*u*u*t*cx1+3*u*t*t*cx2+t*t*t*x2;
    var by=u*u*u*y1+3*u*u*t*cy1+3*u*t*t*cy2+t*t*t*y2;
    var d=Math.sqrt((px-bx)*(px-bx)+(py-by)*(py-by));
    if(d<minD)minD=d;
  }
  return minD;
}

function removeEdge(idx){
  if(idx>=0&&idx<S.edges.length){
    S.edges.splice(idx,1);
    S.selectedEdge=null;
    renderCanvas();
    toast("Connection removed","info");
  }
}

function insertNodeOnEdge(type,x,y,edgeIdx){
  var edge=S.edges[edgeIdx];
  var fromId=edge.from, toId=edge.to;
  S.edges.splice(edgeIdx,1);
  var id="n"+S.nextId++;
  var node={id:id,type:type,x:x,y:y,config:getDefaultConfig(type)};
  S.nodes.push(node);
  S.edges.push({from:fromId,to:id});
  S.edges.push({from:id,to:toId});
  if(type==="sort"&&node.config.field==="__auto__"){
    var cols=getAvailableColumns(id);
    node.config.field=cols.length?cols[0].v:"count";
  }
  renderCanvas();
  selectNode(id);
  hideEmpty();
  toast("Node inserted into connection","ok");
}

function addNode(type,x,y,config){
  var id="n"+S.nextId++;
  var node={id:id,type:type,x:x,y:y,config:config||getDefaultConfig(type)};
  S.nodes.push(node);
  autoConnect(node);
  if(type==="sort"&&node.config.field==="__auto__"){
    var cols=getAvailableColumns(id);
    node.config.field=cols.length?cols[0].v:"count";
  }
  renderCanvas();
  selectNode(id);
  hideEmpty();
  return id;
}

function getDefaultConfig(type){
  switch(type){
    case "filter_salary":return{min:null,max:null,require_salary:true};
    case "filter_score":return{min:0,max:10};
    case "filter_date_range":return{preset:"30d"};
    case "group_by":return{field:"company_name",date_trunc:null};
    case "aggregate":return{functions:[{fn:"count",field:"id",alias:"count_id"}]};
    case "sort":return{field:"__auto__",direction:"desc"};
    case "limit":return{value:20};
    case "chart":return{chart_type:"bar",orientation:"vertical",label_col:"__auto__",value_cols:[]};
    default:return{values:[],mode:"include"};
  }
}

function removeNode(id){
  var incoming=S.edges.filter(function(e){return e.to===id;});
  var outgoing=S.edges.filter(function(e){return e.from===id;});
  S.edges=S.edges.filter(function(e){return e.from!==id&&e.to!==id;});
  incoming.forEach(function(inc){
    outgoing.forEach(function(out){
      if(!S.edges.find(function(e){return e.from===inc.from&&e.to===out.to;}))
        S.edges.push({from:inc.from,to:out.to});
    });
  });
  S.nodes=S.nodes.filter(function(n){return n.id!==id;});
  if(S.selected===id){S.selected=null;S.selectedEdge=null;closeConfig();}
  renderCanvas();
  if(!S.nodes.length)showEmpty();
}

function autoConnect(nn){
  if(S.nodes.length<2)return;
  var prev=S.nodes[S.nodes.length-2];
  if(!S.edges.find(function(e){return e.from===prev.id&&e.to===nn.id;}))
    S.edges.push({from:prev.id,to:nn.id});
}

function getUpstreamNodes(nodeId){
  var result=[],visited={},queue=[nodeId];
  while(queue.length){
    var nid=queue.shift();
    S.edges.forEach(function(e){
      if(e.to===nid&&!visited[e.from]){
        visited[e.from]=true;
        var n=S.nodes.find(function(n){return n.id===e.from;});
        if(n){result.push(n);queue.push(e.from);}
      }
    });
  }
  return result;
}

function getAvailableColumns(nodeId){
  var ups=getUpstreamNodes(nodeId);
  var cols=[];
  var hasGroup=false,hasAgg=false;
  ups.forEach(function(n){
    if(n.type==="group_by"){
      hasGroup=true;
      var f=n.config.field||"company_name";
      if(n.config.date_trunc&&f==="created_at"){
        cols.push({v:"period",l:"Period ("+n.config.date_trunc+")"});
      } else {
        var lbl=VACANCY_FIELDS.find(function(vf){return vf.v===f;});
        cols.push({v:f,l:lbl?lbl.l:f});
      }
    }
    if(n.type==="aggregate"){
      hasAgg=true;
      (n.config.functions||[]).forEach(function(fn){
        var alias=fn.alias||fn.fn+"_"+fn.field;
        cols.push({v:alias,l:fn.fn.toUpperCase()+"("+fn.field+")"});
      });
    }
  });
  if(!hasGroup&&!hasAgg){
    VACANCY_FIELDS.forEach(function(f){cols.push(f);});
    AGG_FIELDS.forEach(function(f){cols.push(f);});
  }
  if(!cols.length)cols.push({v:"count",l:"Count"});
  return cols;
}

function renderCanvas(){
  var inner=document.getElementById("canvasInner");
  inner.querySelectorAll(".c-node").forEach(function(el){el.remove();});
  S.nodes.forEach(function(node){
    var meta=NODE_META[node.type];
    var el=document.createElement("div");
    el.className="c-node"+(S.selected===node.id?" selected":"");
    el.id="cnode-"+node.id;
    el.style.left=node.x+"px";el.style.top=node.y+"px";
    el.innerHTML=
      '<div class="c-node-head">'+
        '<div class="cn-color" style="background:'+meta.color+'"></div>'+
        '<div class="cn-title">'+meta.label+'</div>'+
        '<div class="cn-del" data-del="'+node.id+'">&#x2715;</div>'+
      '</div>'+
      '<div class="c-node-body">'+getNodePreview(node)+'</div>'+
      '<div class="port port-in cat-'+meta.cat+'" data-node="'+node.id+'" data-dir="in"></div>'+
      '<div class="port port-out cat-'+meta.cat+'" data-node="'+node.id+'" data-dir="out"></div>';
    el.addEventListener("mousedown",function(e){
      if(e.target.closest(".cn-del")||e.target.closest(".port"))return;
      startDrag(node.id,e);
    });
    el.addEventListener("click",function(e){if(!e.target.closest(".cn-del"))selectNode(node.id);});
    el.querySelector(".cn-del").addEventListener("click",function(e){e.stopPropagation();removeNode(node.id);});
    el.querySelectorAll(".port").forEach(function(port){
      port.addEventListener("mousedown",function(e){e.stopPropagation();startConnect(port.dataset.node,port.dataset.dir,e);});
    });
    inner.appendChild(el);
  });
  renderEdges();
}

function getNodePreview(node){
  var c=node.config||{};
  switch(node.type){
    case "vacancies_source":return "All vacancies";
    case "companies_source":return "All companies";
    case "filter_salary":
      var p=[];if(c.min)p.push("&ge; $"+Number(c.min).toLocaleString());if(c.max)p.push("&le; $"+Number(c.max).toLocaleString());
      return p.length?p.join(" &amp; "):"Any salary";
    case "filter_score":return "Score "+(c.min||0)+" &ndash; "+(c.max||10);
    case "filter_date_range":return c.preset?c.preset.replace("d"," days"):(c.from||"")+" &rarr; "+(c.to||"");
    case "group_by":return "By "+(c.date_trunc?c.date_trunc+"("+c.field+")":c.field);
    case "aggregate":return(c.functions||[]).map(function(f){return f.fn+"("+f.field+")";}).join(", ")||"count";
    case "sort":var sf=c.field||"auto";if(sf==="__auto__")sf="auto";return"&darr; "+sf+" "+(c.direction||"desc");
    case "limit":return "Top "+(c.value||1000);
    case "chart":var ctp=c.chart_type||"bar";var lc=c.label_col&&c.label_col!=="__auto__"?c.label_col:"auto";return ctp+" &middot; labels: "+lc;
    default:
      if(Array.isArray(c.values)&&c.values.length)
        return c.values.slice(0,3).join(", ")+(c.values.length>3?" +"+(c.values.length-3):"");
      return "Click to configure";
  }
}

function renderEdges(){
  var svg=document.getElementById("connSvg");svg.innerHTML="";
  S.edges.forEach(function(edge,idx){
    var fromEl=document.getElementById("cnode-"+edge.from);
    var toEl=document.getElementById("cnode-"+edge.to);
    if(!fromEl||!toEl)return;
    var x1=fromEl.offsetLeft+fromEl.offsetWidth,y1=fromEl.offsetTop+fromEl.offsetHeight/2;
    var x2=toEl.offsetLeft,y2=toEl.offsetTop+toEl.offsetHeight/2;
    var dx=Math.abs(x2-x1)*0.5;
    var d="M"+x1+","+y1+" C"+(x1+dx)+","+y1+" "+(x2-dx)+","+y2+" "+x2+","+y2;
    // Invisible wide hit-area for clicking
    var hitPath=document.createElementNS("http://www.w3.org/2000/svg","path");
    hitPath.setAttribute("d",d);
    hitPath.classList.add("hit-area");
    hitPath.dataset.edgeIdx=idx;
    hitPath.addEventListener("click",function(ev){
      ev.stopPropagation();
      S.selected=null;S.selectedEdge=idx;closeConfig();renderCanvas();
    });
    svg.appendChild(hitPath);
    // Visible path
    var path=document.createElementNS("http://www.w3.org/2000/svg","path");
    path.setAttribute("d",d);
    path.style.pointerEvents="none";
    if(S.selectedEdge===idx) path.classList.add("selected-edge");
    else if(S.selected&&(edge.from===S.selected||edge.to===S.selected)) path.classList.add("active");
    svg.appendChild(path);
  });
}

function startDrag(nodeId,e){
  var node=S.nodes.find(function(n){return n.id===nodeId;});if(!node)return;
  var sx=e.clientX,sy=e.clientY,ox=node.x,oy=node.y;
  function onMove(ev){
    node.x=ox+(ev.clientX-sx);node.y=oy+(ev.clientY-sy);
    var el=document.getElementById("cnode-"+nodeId);
    if(el){el.style.left=node.x+"px";el.style.top=node.y+"px";}
    renderEdges();
  }
  function onUp(){document.removeEventListener("mousemove",onMove);document.removeEventListener("mouseup",onUp);}
  document.addEventListener("mousemove",onMove);document.addEventListener("mouseup",onUp);
}

function startConnect(nodeId,dir,e){
  var svg=document.getElementById("connSvg");
  var tmp=document.createElementNS("http://www.w3.org/2000/svg","path");
  tmp.setAttribute("stroke","#10b981");tmp.setAttribute("stroke-width","2");
  tmp.setAttribute("stroke-dasharray","6 3");tmp.setAttribute("fill","none");
  tmp.style.pointerEvents="none";
  svg.appendChild(tmp);
  var el=document.getElementById("cnode-"+nodeId);
  var ox=dir==="out"?el.offsetLeft+el.offsetWidth:el.offsetLeft;
  var oy=el.offsetTop+el.offsetHeight/2;
  function onMove(ev){
    var rect=document.getElementById("canvasInner").getBoundingClientRect();
    var mx=ev.clientX-rect.left,my=ev.clientY-rect.top;
    var ddx=Math.abs(mx-ox)*0.4;
    tmp.setAttribute("d","M"+ox+","+oy+" C"+(ox+(dir==="out"?ddx:-ddx))+","+oy+" "+(mx+(dir==="out"?-ddx:ddx))+","+my+" "+mx+","+my);
  }
  function onUp(ev){
    document.removeEventListener("mousemove",onMove);document.removeEventListener("mouseup",onUp);
    tmp.remove();
    var targetPort=findNearestPort(ev.clientX,ev.clientY,nodeId,dir);
    if(targetPort){
      var from=dir==="out"?nodeId:targetPort.id,to=dir==="out"?targetPort.id:nodeId;
      if(!S.edges.find(function(e){return e.from===from&&e.to===to;})){
        S.edges.push({from:from,to:to});
        renderCanvas();
        toast("Connected","ok");
      }
    }
  }
  document.addEventListener("mousemove",onMove);document.addEventListener("mouseup",onUp);
}

function findNearestPort(clientX,clientY,excludeNodeId,excludeDir){
  var best=null,bestDist=40;
  S.nodes.forEach(function(node){
    if(node.id===excludeNodeId)return;
    var el=document.getElementById("cnode-"+node.id);
    if(!el)return;
    var ports=el.querySelectorAll(".port");
    ports.forEach(function(p){
      if(p.dataset.dir===excludeDir)return;
      var r=p.getBoundingClientRect();
      var cx=r.left+r.width/2, cy=r.top+r.height/2;
      var dist=Math.sqrt((clientX-cx)*(clientX-cx)+(clientY-cy)*(clientY-cy));
      if(dist<bestDist){bestDist=dist;best={id:node.id,dir:p.dataset.dir};}
    });
  });
  return best;
}

function selectNode(id){
  S.selected=id;S.selectedEdge=null;renderCanvas();
  var node=S.nodes.find(function(n){return n.id===id;});
  if(node)openConfig(node);
}

function openConfig(node){
  var panel=document.getElementById("configPanel");
  document.getElementById("cfgTitle").textContent=NODE_META[node.type].label+" Settings";
  panel.classList.add("open");
  renderCfg(node);
}

function getNodeDesc(type){
  return NODE_DESC[type]||'';
}

function closeConfig(){
  document.getElementById("configPanel").classList.remove("open");
  document.getElementById("cfgBody").innerHTML="";
}

function renderCfg(node){
  var body=document.getElementById("cfgBody"),c=node.config||{},html="";
  var nid=node.id;
  var desc=getNodeDesc(node.type);
  if(desc)html+='<div class="cfg-group" style="background:rgba(255,255,255,.02);border:1px solid rgba(255,255,255,.04);border-radius:6px;padding:8px;margin-bottom:12px;"><div style="font-size:10px;color:#64748b;line-height:1.5;">'+desc+'</div></div>';
  switch(node.type){
    case "vacancies_source":case "companies_source":
      html='<div class="cfg-group"><div style="font-size:11px;color:#64748b;">Selects all records. Add filters downstream.</div></div>';break;
    case "filter_role":case "filter_domain":case "filter_seniority":case "filter_location":
    case "filter_employment":case "filter_source":case "filter_skill":case "filter_risk":case "filter_company":
      html=buildMultiSel(node);break;
    case "filter_salary":
      html='<div class="cfg-group"><div class="cfg-label">Min Salary (USD)</div><input class="cfg-input" type="number" value="'+(c.min||"")+'" placeholder="100000" onchange="UC(\''+nid+'\',\'min\',this.value?+this.value:null)"></div>'+
        '<div class="cfg-group"><div class="cfg-label">Max Salary (USD)</div><input class="cfg-input" type="number" value="'+(c.max||"")+'" placeholder="300000" onchange="UC(\''+nid+'\',\'max\',this.value?+this.value:null)"></div>'+
        '<div class="cfg-group"><label style="display:flex;align-items:center;gap:6px;font-size:11px;color:#94a3b8;cursor:pointer;"><input type="checkbox" '+(c.require_salary?"checked":"")+' onchange="UC(\''+nid+'\',\'require_salary\',this.checked)" style="accent-color:#10b981;"> Require salary data</label></div>';break;
    case "filter_score":
      html='<div class="cfg-group"><div class="cfg-label">Min Score (0-10)</div><input class="cfg-input" type="number" min="0" max="10" value="'+(c.min!=null?c.min:0)+'" onchange="UC(\''+nid+'\',\'min\',+this.value)"></div>'+
        '<div class="cfg-group"><div class="cfg-label">Max Score (0-10)</div><input class="cfg-input" type="number" min="0" max="10" value="'+(c.max!=null?c.max:10)+'" onchange="UC(\''+nid+'\',\'max\',+this.value)"></div>';break;
    case "filter_date_range":
      html='<div class="cfg-group"><div class="cfg-label">Preset</div><select class="cfg-select" onchange="UC(\''+nid+'\',\'preset\',this.value)">'+
        ["7d","30d","90d","180d","365d"].map(function(v){return'<option value="'+v+'" '+(c.preset===v?"selected":"")+'>'+v.replace("d"," days")+'</option>';}).join("")+
        '<option value="" '+(!c.preset?"selected":"")+'>Custom</option></select></div>';break;
    case "group_by":
      html='<div class="cfg-group"><div class="cfg-label">Group Field</div><select class="cfg-select" onchange="UC(\''+nid+'\',\'field\',this.value);RCB(\''+nid+'\')">'+
        VACANCY_FIELDS.map(function(f){return'<option value="'+f.v+'" '+(c.field===f.v?"selected":"")+'>'+f.l+'</option>';}).join("")+'</select></div>';
      if(c.field==="created_at")
        html+='<div class="cfg-group"><div class="cfg-label">Date Truncation</div><select class="cfg-select" onchange="UC(\''+nid+'\',\'date_trunc\',this.value)">'+
          DATE_TRUNCS.map(function(d){return'<option value="'+d.v+'" '+(c.date_trunc===d.v?"selected":"")+'>'+d.l+'</option>';}).join("")+'</select></div>';
      break;
    case "aggregate":
      html=buildAggCfg(node);break;
    case "sort":
      var sortCols=getAvailableColumns(nid);
      html='<div class="cfg-group"><div class="cfg-label">Sort Field</div><select class="cfg-select" onchange="UC(\''+nid+'\',\'field\',this.value)">'+
        sortCols.map(function(f){return'<option value="'+f.v+'" '+(c.field===f.v?"selected":"")+'>'+f.l+'</option>';}).join('')+'</select>'+
        '<div style="margin-top:4px;font-size:9px;color:#334155">Fields from upstream Group By + Aggregate</div></div>'+
        '<div class="cfg-group"><div class="cfg-label">Direction</div><select class="cfg-select" onchange="UC(\''+nid+'\',\'direction\',this.value)"><option value="desc" '+(c.direction==="desc"?"selected":"")+'>Descending</option><option value="asc" '+(c.direction==="asc"?"selected":"")+'>Ascending</option></select></div>';break;
    case "limit":
      html='<div class="cfg-group"><div class="cfg-label">Max Rows</div><input class="cfg-input" type="number" min="1" max="10000" value="'+(c.value||1000)+'" onchange="UC(\''+nid+'\',\'value\',+this.value)"></div>';break;
    case "chart":
      var chartCols=getAvailableColumns(nid);
      var isPie=c.chart_type==="doughnut"||c.chart_type==="polarArea";
      html='<div class="cfg-group"><div class="cfg-label">Chart Type</div><select class="cfg-select" onchange="UC(\''+nid+'\',\'chart_type\',this.value);RCB(\''+nid+'\')">'+
        ["bar","line","doughnut","polarArea","radar"].map(function(t){return'<option value="'+t+'" '+(c.chart_type===t?"selected":"")+'>'+t[0].toUpperCase()+t.slice(1)+'</option>';}).join("")+'</select></div>';
      html+='<div class="cfg-group"><div class="cfg-label">Label Column (X axis / sectors)</div><select class="cfg-select" onchange="UC(\''+nid+'\',\'label_col\',this.value)">'+
        '<option value="__auto__" '+(c.label_col==="__auto__"?"selected":"")+'>Auto (first column)</option>'+
        chartCols.map(function(f){return'<option value="'+f.v+'" '+(c.label_col===f.v?"selected":"")+'>'+f.l+'</option>';}).join("")+'</select></div>';
      html+='<div class="cfg-group"><div class="cfg-label">Value Column'+(isPie?'':'s')+' (Y axis / size)</div>';
      if(isPie){
        html+='<select class="cfg-select" onchange="UCVC(\''+nid+'\',this.value)">'+
          '<option value="" '+((!c.value_cols||!c.value_cols.length)?"selected":"")+'>Auto (first numeric)</option>'+
          chartCols.map(function(f){return'<option value="'+f.v+'" '+((c.value_cols||[]).indexOf(f.v)>=0?"selected":"")+'>'+f.l+'</option>';}).join("")+'</select>'+
          '<div style="margin-top:4px;font-size:9px;color:#334155">Doughnut/Polar uses one value column for sector sizes</div>';
      } else {
        var vcols=c.value_cols||[];
        html+='<div class="cfg-multi-sel" style="max-height:100px">';
        chartCols.forEach(function(f){
          html+='<label><input type="checkbox" value="'+f.v+'" '+(vcols.indexOf(f.v)>=0?"checked":"")+' onchange="TGVC(\''+nid+'\',this.value,this.checked)"> '+f.l+'</label>';
        });
        html+='</div><div style="margin-top:4px;font-size:9px;color:#334155">Leave empty = all numeric columns</div>';
      }
      html+='</div>';
      if(c.chart_type==="bar"||c.chart_type==="line")
        html+='<div class="cfg-group"><div class="cfg-label">Orientation</div><select class="cfg-select" onchange="UC(\''+nid+'\',\'orientation\',this.value)"><option value="vertical" '+(c.orientation==="vertical"?"selected":"")+'>Vertical</option><option value="horizontal" '+(c.orientation==="horizontal"?"selected":"")+'>Horizontal</option></select></div>';
      break;
    case "table_view":html='<div class="cfg-group"><div style="font-size:11px;color:#64748b;">Displays results as a sortable table.</div></div>';break;
    case "export_csv":html='<div class="cfg-group"><div style="font-size:11px;color:#64748b;">Download results as CSV.</div></div>';break;
  }
  body.innerHTML=html;
}

function buildMultiSel(node){
  var facetMap={filter_role:"roles",filter_domain:"domains",filter_seniority:"seniority",filter_location:"location_types",filter_employment:"employment_types",filter_source:"sources",filter_skill:"skills",filter_risk:"risk_labels",filter_company:"companies"};
  var opts=(S.facets&&S.facets[facetMap[node.type]])||[];
  var sel=node.config.values||[],mode=node.config.mode||"include",nid=node.id;
  var chips=sel.length?sel.map(function(v){return'<span class="cfg-chip" onclick="RC(\''+nid+'\',\''+esc(v)+'\')">'+esc(v)+' <span class="chip-x">&#x2715;</span></span>';}).join(""):'<span style="font-size:10px;color:#334155">None</span>';
  var optHtml=opts.map(function(o){return'<label><input type="checkbox" value="'+esc(o)+'" '+(sel.indexOf(o)>=0?"checked":"")+' onchange="TC(\''+nid+'\',this.value,this.checked)"> '+esc(o)+'</label>';}).join("");
  return'<div class="cfg-group"><div class="cfg-label">Mode</div><select class="cfg-select" onchange="UC(\''+nid+'\',\'mode\',this.value)"><option value="include" '+(mode==="include"?"selected":"")+'>Include</option><option value="exclude" '+(mode==="exclude"?"selected":"")+'>Exclude</option></select></div>'+
    '<div class="cfg-group"><div class="cfg-label">Selected ('+sel.length+')</div><div class="cfg-chips">'+chips+'</div></div>'+
    '<div class="cfg-group"><input class="cfg-input" placeholder="Search..." oninput="FMS(this,\'ms-'+nid+'\')" style="margin-bottom:4px"><div class="cfg-multi-sel" id="ms-'+nid+'">'+(optHtml||'<div style="padding:8px;font-size:10px;color:#334155">No options</div>')+'</div></div>';
}

function buildAggCfg(node){
  var funcs=node.config.functions||[],nid=node.id;
  var rows=funcs.map(function(f,i){
    return'<div style="display:flex;gap:4px;align-items:center;margin-bottom:6px;">'+
      '<select class="cfg-select" style="width:70px" onchange="UA(\''+nid+'\','+i+',\'fn\',this.value)">'+AGG_FNS.map(function(a){return'<option value="'+a+'" '+(f.fn===a?"selected":"")+'>'+a+'</option>';}).join("")+'</select>'+
      '<select class="cfg-select" style="flex:1" onchange="UA(\''+nid+'\','+i+',\'field\',this.value)">'+AGG_FIELDS.map(function(a){return'<option value="'+a.v+'" '+(f.field===a.v?"selected":"")+'>'+a.l+'</option>';}).join("")+'</select>'+
      '<input class="cfg-input" style="width:70px" value="'+(f.alias||"")+'" placeholder="alias" onchange="UA(\''+nid+'\','+i+',\'alias\',this.value)">'+
      '<span style="cursor:pointer;color:#64748b;font-size:14px;" onclick="RA(\''+nid+'\','+i+')">&#x2715;</span></div>';
  }).join("");
  return'<div class="cfg-group"><div class="cfg-label">Functions</div>'+rows+'<button class="tb-btn tb-ghost" style="margin-top:4px;font-size:10px;padding:3px 8px;" onclick="AA(\''+nid+'\')">+ Add</button></div>';
}

/* Globals */
window.UC=function(nid,key,val){var n=S.nodes.find(function(n){return n.id===nid;});if(n){n.config[key]=val;renderCanvas();}};
window.RCB=function(nid){var n=S.nodes.find(function(n){return n.id===nid;});if(n)renderCfg(n);};
window.TC=function(nid,val,chk){
  var n=S.nodes.find(function(n){return n.id===nid;});if(!n)return;
  var a=n.config.values||[];
  if(chk&&a.indexOf(val)<0)a.push(val);
  if(!chk){var i=a.indexOf(val);if(i>=0)a.splice(i,1);}
  n.config.values=a;renderCfg(n);renderCanvas();
};
window.RC=function(nid,val){TC(nid,val,false);};
window.FMS=function(inp,id){var q=inp.value.toLowerCase();document.querySelectorAll("#"+id+" label").forEach(function(l){l.style.display=l.textContent.toLowerCase().indexOf(q)>=0?"":"none";});};
window.UA=function(nid,i,k,v){var n=S.nodes.find(function(n){return n.id===nid;});if(!n)return;n.config.functions[i][k]=v;if(k==="fn"||k==="field"){var f=n.config.functions[i];f.alias=f.fn+"_"+f.field;}renderCanvas();renderCfg(n);};
window.AA=function(nid){var n=S.nodes.find(function(n){return n.id===nid;});if(!n)return;n.config.functions.push({fn:"avg",field:"salary_min_usd",alias:"avg_salary_min_usd"});renderCfg(n);};
window.RA=function(nid,i){var n=S.nodes.find(function(n){return n.id===nid;});if(!n)return;n.config.functions.splice(i,1);renderCfg(n);renderCanvas();};
window.UCVC=function(nid,val){var n=S.nodes.find(function(n){return n.id===nid;});if(!n)return;n.config.value_cols=val?[val]:[];renderCanvas();};
window.TGVC=function(nid,val,chk){var n=S.nodes.find(function(n){return n.id===nid;});if(!n)return;var a=n.config.value_cols||[];if(chk&&a.indexOf(val)<0)a.push(val);if(!chk){var i=a.indexOf(val);if(i>=0)a.splice(i,1);}n.config.value_cols=a;renderCanvas();};

/* Run */
window.runPipeline=async function(){
  if(!S.nodes.length){toast("Add nodes first","err");return;}
  var hasSource=S.nodes.some(function(n){return n.type==="vacancies_source"||n.type==="companies_source";});
  if(!hasSource){toast("Pipeline needs a Source node (Vacancies or Companies)","err",5000);return;}
  var hasAgg=S.nodes.some(function(n){return n.type==="aggregate";});
  var hasGroup=S.nodes.some(function(n){return n.type==="group_by";});
  if(hasAgg&&!hasGroup){toast("Aggregate needs a Group By node upstream","err",5000);return;}
  var btn=document.getElementById("btnRun");btn.disabled=true;btn.textContent="Running\u2026";
  try{
    var payload={nodes:S.nodes.map(function(n){return{id:n.id,type:n.type,config:n.config};}),edges:S.edges};
    var res=await fetch("/api/analytics/execute",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});
    if(!res.ok){var err=await res.json().catch(function(){return{detail:res.statusText};});throw new Error(err.detail||"Query failed");}
    S.results=await res.json();
    showResults(S.results);
    toast(S.results.row_count+" rows","ok");
  }catch(e){
    var msg=e.message||"Unknown error";
    if(msg.indexOf("Query error")>=0){
      msg=msg.replace("Query error: ","");
      if(msg.indexOf("column")>=0&&msg.indexOf("does not exist")>=0){
        var m=msg.match(/column "([^"]+)"/);
        msg=m?"Column '"+m[1]+"' not found. Check Sort/Group By fields match your Aggregate aliases.":msg;
      }
    }
    toast(msg,"err",6000);
  }
  finally{btn.disabled=false;btn.innerHTML="&#9654; Run Pipeline";}
};

function showResults(data){
  document.getElementById("resultsPanel").classList.remove("collapsed");
  document.getElementById("resultsTitle").textContent="Results \u2014 "+data.row_count+" rows";
  var content=document.getElementById("resultsContent"),html="";
  var oNode=S.nodes.find(function(n){return["chart","table_view","export_csv"].indexOf(n.type)>=0;});
  var oType=oNode?oNode.type:"table",oCfg=oNode?oNode.config:{};
  if(oType==="chart"&&data.columns.length>=2) html+='<div class="results-chart"><canvas id="resultsChart"></canvas></div>';
  if(oType==="export_csv") html+='<div style="margin-bottom:12px;"><button class="tb-btn tb-ghost" onclick="dlCSV()">&#x1F4BE; Download CSV</button></div>';
  html+=buildTable(data);
  if(data.sql_preview) html+='<div class="sql-preview">'+esc(data.sql_preview)+'</div>';
  content.innerHTML=html;
  if(oType==="chart"&&data.columns.length>=2) requestAnimationFrame(function(){renderChart(data,oCfg);});
}

function buildTable(data){
  if(!data.rows.length)return'<div style="padding:16px;text-align:center;font-size:12px;color:#475569;">No data</div>';
  var h='<div style="max-height:300px;overflow:auto;border:1px solid rgba(255,255,255,.04);border-radius:8px;"><table class="results-table"><thead><tr>';
  data.columns.forEach(function(c){h+='<th>'+esc(c)+'</th>';});
  h+='</tr></thead><tbody>';
  data.rows.forEach(function(row){
    h+='<tr>';row.forEach(function(v,i){
      var d=v===null?'\u2014':String(v);
      if(typeof v==='number'&&data.columns[i].indexOf('salary')>=0)d='$'+Number(v).toLocaleString(undefined,{maximumFractionDigits:0});
      else if(typeof v==='number'&&!Number.isInteger(v))d=Number(v).toLocaleString(undefined,{maximumFractionDigits:1});
      h+='<td>'+esc(d)+'</td>';
    });h+='</tr>';
  });
  return h+'</tbody></table></div>';
}

function renderChart(data,cfg){
  if(S.chartInstance){S.chartInstance.destroy();S.chartInstance=null;}
  var cv=document.getElementById("resultsChart");if(!cv)return;
  var ct=cfg.chart_type||"bar";
  var isPie=ct==="doughnut"||ct==="polarArea";
  var pal=["#10b981","#3b82f6","#8b5cf6","#f59e0b","#ef4444","#06b6d4","#ec4899","#84cc16","#14b8a6","#a855f7","#f97316","#22d3ee"];

  // Determine label column index
  var labelIdx=0;
  if(cfg.label_col&&cfg.label_col!=="__auto__"){
    var li=data.columns.indexOf(cfg.label_col);
    if(li>=0)labelIdx=li;
  }

  // Determine value column indices
  var valIdxs=[];
  if(cfg.value_cols&&cfg.value_cols.length){
    cfg.value_cols.forEach(function(vc){
      var vi=data.columns.indexOf(vc);
      if(vi>=0&&vi!==labelIdx)valIdxs.push(vi);
    });
  }
  if(!valIdxs.length){
    for(var i=0;i<data.columns.length;i++){
      if(i===labelIdx)continue;
      var allNum=data.rows.every(function(r){return r[i]===null||!isNaN(Number(r[i]));});
      if(allNum)valIdxs.push(i);
    }
  }
  if(!valIdxs.length&&data.columns.length>1){valIdxs=[labelIdx===0?1:0];}

  var labels=data.rows.map(function(r){return r[labelIdx]!=null?String(r[labelIdx]):"\u2014";});
  var ds=[];

  if(isPie){
    var vi=valIdxs[0]||1;
    var vals=data.rows.map(function(r){return r[vi]!=null?Number(r[vi]):0;});
    var bgColors=labels.map(function(_,i){return pal[i%pal.length]+"cc";});
    var borderColors=labels.map(function(_,i){return pal[i%pal.length];});
    ds.push({label:data.columns[vi],data:vals,backgroundColor:bgColors,borderColor:borderColors,borderWidth:2});
  } else {
    valIdxs.forEach(function(vi,idx){
      var vals=data.rows.map(function(r){return r[vi]!=null?Number(r[vi]):0;});
      var col=pal[idx%pal.length];
      ds.push({
        label:data.columns[vi],data:vals,
        backgroundColor:ct==="line"?"transparent":col+"cc",
        borderColor:col,borderWidth:2,
        borderRadius:ct==="bar"?4:0,
        tension:0.3,fill:ct==="line",
        pointRadius:ct==="line"?3:0
      });
    });
  }

  var isH=cfg.orientation==="horizontal"&&ct==="bar";
  var noAxes=isPie||ct==="radar";
  S.chartInstance=new Chart(cv,{type:ct,data:{labels:labels,datasets:ds},options:{
    responsive:true,maintainAspectRatio:false,indexAxis:isH?"y":"x",
    plugins:{
      legend:{display:true,labels:{color:"#94a3b8",font:{size:10}}},
      tooltip:{callbacks:{label:function(ctx){
        var v=ctx.parsed!==undefined?(typeof ctx.parsed==="object"?(ctx.parsed.y||ctx.parsed.x||ctx.parsed):ctx.parsed):ctx.raw;
        return ctx.dataset.label+": "+Number(v).toLocaleString(undefined,{maximumFractionDigits:1});
      }}}
    },
    scales:noAxes?{}:{x:{title:{display:true,text:data.columns[labelIdx],color:"#475569",font:{size:10}},ticks:{color:"#64748b",font:{size:10},maxRotation:45},grid:{color:"rgba(255,255,255,.04)"}},y:{beginAtZero:true,title:{display:true,text:valIdxs.map(function(i){return data.columns[i];}).join(", "),color:"#475569",font:{size:10}},ticks:{color:"#64748b",font:{size:10}},grid:{color:"rgba(255,255,255,.04)"}}}
  }});
}

window.dlCSV=function(){
  if(!S.results)return;
  var c=S.results.columns,r=S.results.rows;
  var csv=c.join(",")+"\n";
  r.forEach(function(row){csv+=row.map(function(v){return v===null?'':'"'+String(v).replace(/"/g,'""')+'"';}).join(",")+"\n";});
  var a=document.createElement('a');a.href=URL.createObjectURL(new Blob([csv],{type:'text/csv'}));
  a.download='hirelens_'+new Date().toISOString().slice(0,10)+'.csv';a.click();
};

/* Templates */
var TPL={
  salary_by_role:{nodes:[
    {type:"vacancies_source",x:60,y:120,config:{}},
    {type:"filter_salary",x:280,y:120,config:{min:null,max:null,require_salary:true}},
    {type:"group_by",x:500,y:120,config:{field:"role",date_trunc:null}},
    {type:"aggregate",x:720,y:120,config:{functions:[{fn:"avg",field:"salary_min_usd",alias:"avg_min"},{fn:"avg",field:"salary_max_usd",alias:"avg_max"},{fn:"count",field:"id",alias:"count"}]}},
    {type:"sort",x:940,y:120,config:{field:"avg_max",direction:"desc"}},
    {type:"limit",x:1140,y:120,config:{value:15}},
    {type:"chart",x:1340,y:120,config:{chart_type:"bar",orientation:"horizontal",label_col:"role",value_cols:["avg_min","avg_max"]}}]},
  hiring_trends:{nodes:[
    {type:"vacancies_source",x:60,y:120,config:{}},
    {type:"filter_date_range",x:280,y:120,config:{preset:"90d"}},
    {type:"group_by",x:500,y:120,config:{field:"created_at",date_trunc:"week"}},
    {type:"aggregate",x:720,y:120,config:{functions:[{fn:"count",field:"id",alias:"vacancies"}]}},
    {type:"sort",x:940,y:120,config:{field:"period",direction:"asc"}},
    {type:"chart",x:1140,y:120,config:{chart_type:"line",orientation:"vertical",label_col:"period",value_cols:["vacancies"]}}]},
  top_companies:{nodes:[
    {type:"vacancies_source",x:60,y:120,config:{}},
    {type:"group_by",x:280,y:120,config:{field:"company_name",date_trunc:null}},
    {type:"aggregate",x:500,y:120,config:{functions:[{fn:"count",field:"id",alias:"vacancies"},{fn:"avg",field:"ai_score_value",alias:"avg_score"}]}},
    {type:"sort",x:720,y:120,config:{field:"vacancies",direction:"desc"}},
    {type:"limit",x:920,y:120,config:{value:20}},
    {type:"chart",x:1120,y:120,config:{chart_type:"bar",orientation:"horizontal",label_col:"company_name",value_cols:["vacancies"]}}]},
  skills_demand:{nodes:[
    {type:"vacancies_source",x:60,y:120,config:{}},
    {type:"filter_date_range",x:280,y:120,config:{preset:"90d"}},
    {type:"group_by",x:500,y:120,config:{field:"role",date_trunc:null}},
    {type:"aggregate",x:720,y:120,config:{functions:[{fn:"count",field:"id",alias:"demand"}]}},
    {type:"sort",x:940,y:120,config:{field:"demand",direction:"desc"}},
    {type:"limit",x:1140,y:120,config:{value:20}},
    {type:"chart",x:1340,y:120,config:{chart_type:"bar",orientation:"horizontal",label_col:"role",value_cols:["demand"]}}]},
  remote_analysis:{nodes:[
    {type:"vacancies_source",x:60,y:120,config:{}},
    {type:"group_by",x:280,y:120,config:{field:"location_type",date_trunc:null}},
    {type:"aggregate",x:500,y:120,config:{functions:[{fn:"count",field:"id",alias:"count"},{fn:"avg",field:"salary_min_usd",alias:"avg_salary"}]}},
    {type:"chart",x:720,y:120,config:{chart_type:"doughnut",orientation:"vertical",label_col:"location_type",value_cols:["count"]}}]},
  seniority_salary:{nodes:[
    {type:"vacancies_source",x:60,y:120,config:{}},
    {type:"filter_salary",x:280,y:120,config:{min:null,max:null,require_salary:true}},
    {type:"group_by",x:500,y:120,config:{field:"seniority",date_trunc:null}},
    {type:"aggregate",x:720,y:120,config:{functions:[{fn:"avg",field:"salary_min_usd",alias:"avg_min"},{fn:"avg",field:"salary_max_usd",alias:"avg_max"},{fn:"count",field:"id",alias:"count"}]}},
    {type:"sort",x:940,y:120,config:{field:"avg_max",direction:"desc"}},
    {type:"chart",x:1140,y:120,config:{chart_type:"bar",orientation:"vertical",label_col:"seniority",value_cols:["avg_min","avg_max"]}}]},
  domain_overview:{nodes:[
    {type:"vacancies_source",x:60,y:120,config:{}},
    {type:"group_by",x:280,y:120,config:{field:"domain",date_trunc:null}},
    {type:"aggregate",x:500,y:120,config:{functions:[{fn:"count",field:"id",alias:"vacancies"},{fn:"avg",field:"salary_min_usd",alias:"avg_salary"}]}},
    {type:"sort",x:720,y:120,config:{field:"vacancies",direction:"desc"}},
    {type:"limit",x:920,y:120,config:{value:15}},
    {type:"chart",x:1120,y:120,config:{chart_type:"bar",orientation:"vertical",label_col:"domain",value_cols:["vacancies","avg_salary"]}}]},
};

window.loadTemplate=function(key){
  var tpl=TPL[key];if(!tpl)return;
  S.nodes=[];S.edges=[];S.selected=null;S.nextId=1;closeConfig();
  tpl.nodes.forEach(function(nd,i){
    var id="n"+S.nextId++;
    S.nodes.push({id:id,type:nd.type,x:nd.x,y:nd.y,config:JSON.parse(JSON.stringify(nd.config))});
    if(i>0)S.edges.push({from:S.nodes[i-1].id,to:id});
  });
  renderCanvas();hideEmpty();closeTplMenu();toast("Template loaded \u2014 click Run","info");
};

/* Helpers */
function hideEmpty(){document.getElementById("emptyState").style.display="none";}
function showEmpty(){document.getElementById("emptyState").style.display="flex";}
window.toggleResults=function(){document.getElementById("resultsPanel").classList.toggle("collapsed");};
window.toggleTemplates=function(){document.getElementById("tplMenu").classList.toggle("open");};
function closeTplMenu(){document.getElementById("tplMenu").classList.remove("open");}
document.addEventListener("click",function(e){if(!e.target.closest(".tpl-dropdown"))closeTplMenu();});
window.clearCanvas=function(){
  S.nodes=[];S.edges=[];S.selected=null;S.nextId=1;S.results=null;closeConfig();
  document.getElementById("resultsPanel").classList.add("collapsed");
  document.getElementById("resultsContent").innerHTML="";
  renderCanvas();showEmpty();
};

function toast(msg,type,dur){var el=document.createElement("div");el.className="studio-toast toast-"+(type||"info");el.textContent=msg;document.body.appendChild(el);setTimeout(function(){el.remove();},dur||3000);}
function esc(s){var d=document.createElement("div");d.textContent=String(s);return d.innerHTML;}

})();
