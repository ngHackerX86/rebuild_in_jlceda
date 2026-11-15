const g = await eda.pcb_ManufactureData.getGerberFile("REBUILD_IN_JLC");
if (!g) return { ok: false, reason: "getGerberFile 返回空" };
const buf = new Uint8Array(await g.arrayBuffer());
let bin = "";
const CH = 0x8000;
for (let i = 0; i < buf.length; i += CH) bin += String.fromCharCode.apply(null, buf.subarray(i, i + CH));
const p = await eda.dmt_Project.getCurrentProjectInfo().catch(() => null);
return { ok: true, size: buf.length, b64: btoa(bin), project: p ? (p.name || p.friendlyName) : null };
