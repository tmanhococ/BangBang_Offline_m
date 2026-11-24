// Local account panel. Authentication/CSRF stay owned by admin.js. Passwords
// live only in the current form/memory; pending storage is a strict public ID.
const $=id=>document.getElementById(id);
const STORE='bangbang.m31.pending-account.v1';
const LABELS={create:'Tạo tài khoản',password:'Đổi mật khẩu',delete:'Xóa mềm (giữ dữ liệu)',restore:'Khôi phục tài khoản'};
const state={authenticated:false,selected:null,pending:null,payload:null,proof:null,busy:false,storageFault:false,offset:0,total:0,epoch:0,generation:0};
let core=null;
const node=(tag,text,className)=>{const el=document.createElement(tag);if(text!=null)el.textContent=text;if(className)el.className=className;return el;};
const message=(text,error=false)=>{const el=$('account-message');el.textContent=text||'';el.classList.toggle('is-error',error);};
const id=()=>globalThis.crypto.randomUUID();
const blocked=()=>state.busy||state.storageFault||!!state.pending||!state.authenticated||core?.canChange?.()===false;
function wipeSecrets(){state.payload=null;state.proof=null;$('account-password').value='';$('account-password-repeat').value='';$('account-confirm-panel').hidden=true;$('account-delete-confirm').value='';}
function publicIntent(payload){return {request_id:payload.request_id,action:payload.action,username:payload.username||payload.target.username,profile_id:payload.target?.profile_id??null};}
function validPending(value){return value&&typeof value==='object'&&!Array.isArray(value)&&Object.keys(value).sort().join(',')==='action,profile_id,request_id,username'
  &&typeof value.request_id==='string'&&/^[A-Za-z0-9_-]{1,64}$/.test(value.request_id)&&Object.hasOwn(LABELS,value.action)
  &&typeof value.username==='string'&&value.username.length>0&&value.username.length<=128
  &&(value.profile_id===null||Number.isInteger(value.profile_id)&&value.profile_id>0&&value.profile_id<=0xffffffff);}
function persist(){const value=JSON.stringify(state.pending);sessionStorage.setItem(STORE,value);if(sessionStorage.getItem(STORE)!==value)throw new Error('Không lưu được mã thao tác tài khoản. Chưa gửi thay đổi; giữ nguyên tab và kiểm tra bộ nhớ trang.');}
function clearPending(){sessionStorage.removeItem(STORE);if(sessionStorage.getItem(STORE)!==null)throw new Error('Server đã xác nhận kết quả nhưng chưa xóa được mã chờ trên tab. Bấm Kiểm tra kết quả để thử lại, không tạo thao tác khác.');state.pending=null;wipeSecrets();}
function restorePending(){try{const value=sessionStorage.getItem(STORE);if(value!==null){const parsed=JSON.parse(value);if(!validPending(parsed))throw new Error('invalid');state.pending=parsed;}}catch{state.storageFault=true;message('Không đọc được mã thao tác tài khoản cũ. Chưa cho thay đổi tài khoản để tránh lặp; cho phép bộ nhớ trang rồi tải lại.',true);}if(state.pending||state.storageFault)$('account-panel').open=true;renderPending();}
function renderPending(){
  $('account-pending').hidden=!state.pending;
  if(state.pending){$('account-pending-label').textContent=`${LABELS[state.pending.action]} · ${state.pending.username}${state.pending.profile_id!==null?` · ID ${state.pending.profile_id}`:''}. Kiểm tra kết quả hoặc hủy thao tác chưa thực hiện; không tự gửi lại mật khẩu.`;$('account-pending-id').textContent=state.pending.request_id;}
  sync();
}
function sync(){
  const lock=blocked();
  for(const el of document.querySelectorAll('#account-form input,#account-form select,#account-form button,#account-results button'))el.disabled=lock;
  $('account-new').disabled=lock;$('account-preview').textContent=state.busy?'Đang xử lý…':'Kiểm tra trước khi thay đổi';
  const canConfirm=state.authenticated&&!state.busy&&!state.storageFault&&!!state.pending&&!!state.payload&&!!state.proof&&core?.canChange?.()!==false;
  $('account-confirm').disabled=!canConfirm||(state.pending?.action==='delete'&&$('account-delete-confirm').value!=='XOA '+state.pending.username);
  $('account-delete-confirm').disabled=!canConfirm;
  $('account-check').disabled=!state.authenticated||state.busy||!state.pending||state.storageFault;
  $('account-cancel').disabled=$('account-check').disabled;
  $('account-prev').disabled=state.offset<=0;$('account-next').disabled=state.offset+25>=state.total;
}
function formMode(){
  const action=$('account-action').value,create=action==='create';
  $('account-edit-title').textContent=LABELS[action];$('account-password-fields').hidden=!['create','password'].includes(action);
  $('account-password').required=$('account-password-repeat').required=['create','password'].includes(action);
  $('account-username').readOnly=!create;
  if($('account-buff-field'))$('account-buff-field').hidden=!create;
  if(!create&&state.selected){$('account-username').value=state.selected.username;$('account-selected').textContent=`${state.selected.name||'Chưa tạo nhân vật'} · ${state.selected.profile_id===null?'Chưa có ID nhân vật':`ID ${state.selected.profile_id}`} · ${state.selected.deleted?'Đã xóa mềm':'Đang hoạt động'}`;}
  else if(create){$('account-selected').textContent=$('account-buff')?.checked?'Tài khoản sẽ được buff: Cấp 36, VIP 6, có sẵn full tank và xu/bạc.':'Tài khoản thường: Cấp 1, VIP 0, 3 tank cơ bản khởi đầu.';}
  sync();
}
function newAccount(){if(blocked())return;state.selected=null;$('account-action').value='create';$('account-username').value='';wipeSecrets();formMode();}
function selectAccount(row){if(blocked())return;state.selected={...row};wipeSecrets();$('account-action').value=row.deleted?'restore':'password';formMode();}
async function loadAccounts(reset=false){
  if(!state.authenticated||!core)return;if(reset)state.offset=0;const epoch=++state.epoch,generation=state.generation,offset=state.offset;
  $('account-results').replaceChildren(node('p','Đang đọc tài khoản…','empty'));
  try{const reply=await core.api(`accounts?${new URLSearchParams({q:$('account-query').value.trim(),status:$('account-status').value||'active',offset,limit:25})}`);
    if(epoch!==state.epoch||generation!==state.generation||!state.authenticated)return;
    const box=$('account-results');box.replaceChildren();state.total=reply.total;
    for(const row of reply.rows){const button=node('button',null,'person');button.type='button';button.append(node('strong',row.username),node('span',`${row.name||'Chưa tạo nhân vật'}${row.profile_id!==null?` · ID ${row.profile_id}`:''}`),node('span',row.deleted?'Đã xóa mềm · Giữ dữ liệu':row.online?'Đang mở game':row.in_progress?'Còn trận đang lưu':'Đã đóng game',row.deleted?'account-deleted':''));button.addEventListener('click',()=>selectAccount(row));box.append(button);}
    if(!reply.rows.length)box.append(node('p','Không có tài khoản khớp bộ lọc.','empty'));
    $('account-count').textContent=reply.total?`${offset+1}–${offset+reply.rows.length} / ${reply.total}`:'0 tài khoản';sync();
  }catch(error){if(epoch===state.epoch&&generation===state.generation)$('account-results').replaceChildren(node('p',error.message,'empty'));}
}
function payload(){
  const action=$('account-action').value;if(!Object.hasOwn(LABELS,action))throw new Error('Chọn thao tác hợp lệ.');
  const result={action,request_id:id()};
  if(action==='create'){
    result.username=$('account-username').value.trim();
    if(result.username!=='1'&&(result.username.length<3||result.username.length>32))throw new Error('Tên đăng nhập cần 3–32 ký tự.');
    result.buff=!!$('account-buff')?.checked;
  }
  else{if(!state.selected)throw new Error('Chọn tài khoản ở danh sách trước.');result.target={username:state.selected.username,profile_id:state.selected.profile_id,revision:state.selected.revision};}
  if(['create','password'].includes(action)){const username=result.username||result.target.username;result.password=$('account-password').value;
    if(result.password!==$('account-password-repeat').value)throw new Error('Hai lần nhập mật khẩu chưa giống nhau.');
    if(result.password.length<(username==='1'?1:6)||result.password.length>256)throw new Error('Mật khẩu cần ít nhất 6 ký tự (tài khoản test 1 giữ ngoại lệ), tối đa 256 ký tự.');}
  return result;
}
function showPreview(reply){
  $('account-confirm-title').textContent=reply.label;$('account-confirm-target').textContent=`${reply.account.username}${reply.account.profile_id!==null?` · ${reply.account.name||'Nhân vật'} · ID ${reply.account.profile_id}`:''}`;
  $('account-warnings').replaceChildren(...reply.warnings.map(text=>node('li',text)));
  const deleting=reply.action==='delete';$('account-confirm-panel').classList.toggle('is-delete',deleting);$('account-delete-label').hidden=$('account-delete-confirm').hidden=!deleting;
  $('account-delete-label').textContent=deleting?`Gõ chính xác: XOA ${reply.account.username}`:'';$('account-delete-confirm').value='';$('account-confirm-panel').hidden=false;sync();
}
async function finish(reply){
  if(!state.pending||reply.request_id!==state.pending.request_id||!['completed','cancelled'].includes(reply.status))throw new Error('Biên nhận không khớp thao tác đang chờ.');
  if(reply.status==='completed'&&(reply.action!==state.pending.action||reply.result?.operation!==state.pending.action
    ||reply.result?.account?.username!==state.pending.username||reply.result?.account?.profile_id!==state.pending.profile_id))throw new Error('Nội dung biên nhận không khớp tài khoản đang chờ.');
  const result=reply.result;clearPending();renderPending();
  message(reply.status==='completed'?result?.message||'Đã hoàn tất thao tác tài khoản.':'Đã hủy thao tác. Yêu cầu đến muộn với mã cũ sẽ không thay đổi tài khoản.');
  state.selected=null;$('account-action').value='create';$('account-username').value='';formMode();
  if(reply.status==='completed')core.onChanged?.(result);
  await loadAccounts();
}
async function preview(){
  if(blocked())return;let intent;try{intent=payload();}catch(error){message(error.message,true);return;}
  state.pending=publicIntent(intent);state.payload=intent;
  try{persist();}catch(error){state.storageFault=true;wipeSecrets();renderPending();message(error.message,true);return;}
  state.busy=true;renderPending();message('Đang kiểm tra tài khoản…');const generation=state.generation;
  try{const reply=await core.api('accounts/preview',{method:'POST',body:intent});
    if(generation!==state.generation||!state.authenticated)return;
    if(reply.request_id!==state.pending.request_id)throw new Error('Phản hồi không khớp mã thao tác.');
    if(reply.status==='completed'){await finish(reply);return;}
    if(reply.status!=='prepared'||!reply.preview_token)throw new Error('Server chưa xác nhận nội dung kiểm tra.');
    state.proof=reply.preview_token;showPreview(reply);message('Kiểm tra đúng tài khoản và thao tác, rồi bấm Xác nhận thực hiện.');
  }catch(error){wipeSecrets();message(`${error.message} Chưa tự thực hiện hoặc gửi lại. Kiểm tra kết quả/hủy thao tác đang chờ trước khi nhập lại.`,true);}
  finally{state.busy=false;renderPending();}
}
async function execute(){
  if($('account-confirm').disabled||!state.payload||!state.proof)return;
  try{persist();}catch(error){state.storageFault=true;wipeSecrets();renderPending();message(error.message,true);return;}
  const body={intent:state.payload,preview_token:state.proof};if(state.payload.action==='delete')body.confirmation=$('account-delete-confirm').value;
  state.busy=true;sync();message('Đang xác nhận thay đổi tài khoản…');
  try{const reply=await core.api('accounts/execute',{method:'POST',body,timeout:20000});await finish(reply);}
  catch(error){message(`${error.message} Không tự gửi lại mật khẩu. Bấm Kiểm tra kết quả; nếu chưa thực hiện, có thể hủy mã cũ rồi nhập lại.`,true);}
  finally{wipeSecrets();state.busy=false;renderPending();}
}
async function resolve(cancel=false){
  if(!state.authenticated||state.busy||!state.pending||state.storageFault)return;
  state.busy=true;sync();
  try{const request_id=state.pending.request_id,reply=cancel?await core.api('accounts/cancel',{method:'POST',body:{request_id}}):await core.api(`accounts/receipt?${new URLSearchParams({request_id})}`);
    if(reply.request_id!==request_id)throw new Error('Biên nhận không khớp mã thao tác.');
    if(['completed','cancelled'].includes(reply.status)){await finish(reply);return;}
    if(!['prepared','unknown'].includes(reply.status))throw new Error('Trạng thái biên nhận không hợp lệ.');
    message(reply.status==='prepared'?'Server có yêu cầu đã kiểm tra, chưa xác nhận hoàn tất. Có thể xác nhận ở phần bên dưới nếu vẫn còn phiên xem trước, hoặc hủy rồi nhập lại.':'Chưa tìm thấy biên nhận. Không gửi lại dưới mã mới: bấm Hủy thao tác chưa thực hiện để khóa cả yêu cầu đến muộn.');
  }catch(error){message(error.message,true);}finally{state.busy=false;renderPending();}
}
function setAuthenticated(value){state.authenticated=value;state.generation++;if(!value)wipeSecrets();else{loadAccounts();if(state.pending)resolve();}sync();}
function connect(value){core=value;restorePending();formMode();}
$('account-form').addEventListener('submit',event=>{event.preventDefault();preview();});
$('account-search').addEventListener('submit',event=>{event.preventDefault();loadAccounts(true);});
$('account-status').addEventListener('change',()=>loadAccounts(true));
$('account-new').addEventListener('click',newAccount);
$('account-buff')?.addEventListener('change',formMode);
$('account-action').addEventListener('change',()=>{wipeSecrets();if($('account-action').value==='create')$('account-username').value='';formMode();});
$('account-confirm').addEventListener('click',execute);$('account-delete-confirm').addEventListener('input',sync);
$('account-check').addEventListener('click',()=>resolve());$('account-cancel').addEventListener('click',()=>resolve(true));
$('account-prev').addEventListener('click',()=>{state.offset=Math.max(0,state.offset-25);loadAccounts();});$('account-next').addEventListener('click',()=>{state.offset+=25;loadAccounts();});
for(const link of document.querySelectorAll('a[href="#account-panel"]'))link.addEventListener('click',()=>{$('account-panel').open=true;});
globalThis.bbAdminAccounts={connect,setAuthenticated,sync};
