// J в Minecraft (Java Edition), версия 2: приказы, очередь задач, планирование, прерывания по опасности.
//
// Запуск:  npm install   →   node j_bot.js     (мост должен работать: python j_mc_bridge.py)
// PowerShell:  $env:MC_VERSION="1.12.2"; $env:MC_OWNER="ТвойНик"; node j_bot.js
// Переменные окружения (все необязательные):
//   MC_HOST=localhost  MC_PORT=25565  MC_USERNAME=J  MC_VERSION=1.12.2  MC_OWNER=ТвойНик
//
// В игровом чате:  "j руби 10 дерева", "j добудь камень", "j атакуй зомби", "j одень броню",
//                  "j скажи здоровье", "j покушай", "j за мной", "j стой", "j ещё", "j лесоповал значит рубить дерево"
//
// Что это за «планирование»: каждый приказ превращается в задачу, задачи идут по очереди.
// Перед боем J сама надевает броню и берёт оружие, перед добычей берёт нужный инструмент.
// Опасность (низкое здоровье, крипер рядом, огонь) прерывает всё с приоритетом.

const fs = require('fs')
const path = require('path')
const mineflayer = require('mineflayer')
const { pathfinder, Movements, goals } = require('mineflayer-pathfinder')

function loadEnvFile() {
  const envPath = process.env.J_ENV || path.join(__dirname, 'j.env')
  if (!fs.existsSync(envPath)) return
  let lines = []
  try {
    lines = fs.readFileSync(envPath, 'utf8').replace(/^\uFEFF/, '').split(/\r?\n/)
  } catch {
    return
  }
  const seen = new Set()
  for (const raw of lines) {
    const line = raw.trim()
    if (!line || line.startsWith('#') || !line.includes('=')) continue
    const idx = line.indexOf('=')
    const key = line.slice(0, idx).trim()
    let val = line.slice(idx + 1).trim()
    if ((val.startsWith('"') && val.endsWith('"')) || (val.startsWith("'") && val.endsWith("'"))) {
      val = val.slice(1, -1)
    }
    if (!key || seen.has(key)) continue
    seen.add(key)
    if (process.env[key] === undefined) process.env[key] = val
  }
}
loadEnvFile()

const HOST = process.env.MC_HOST || 'localhost'
const PORT = parseInt(process.env.MC_PORT || '25565', 10)
const USERNAME = process.env.MC_USERNAME || 'J'
const VERSION = process.env.MC_VERSION || undefined // undefined = определить автоматически
const AUTH = (process.env.MC_AUTH || 'offline').trim() || 'offline'
const SKIN = (process.env.MC_SKIN || '').trim()
const OWNER = process.env.MC_OWNER || null
const BRIDGE = (process.env.J_CORE_URL || ('http://127.0.0.1:' + (process.env.J_PORT || '8765'))).replace(/\/+$/, '')
const DATA_DIR = process.env.J_DATA_DIR || __dirname
const PLACES_FILE = process.env.J_PLACES || path.join(DATA_DIR, 'j_places.json')

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
const keyOf = (p) => `${p.x},${p.y},${p.z}`

const bot = mineflayer.createBot({ host: HOST, port: PORT, username: USERNAME, version: VERSION, auth: AUTH })
bot.loadPlugin(pathfinder)

// ======================= связь с мозгом J (Python) =======================
async function post(path, payload) {
  try {
    const res = await fetch(BRIDGE + path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    return await res.json()
  } catch (err) {
    console.log('[мост недоступен]', err.message)
    return {}
  }
}

// Безопасная отправка в чат (лимит 256 символов, антиспам)
const chatQueue = []
let sending = false
function say(text) {
  for (const line of String(text).split('\n')) {
    for (const chunk of line.match(/.{1,230}/g) || []) chatQueue.push(chunk)
  }
  pump()
}
async function pump() {
  if (sending) return
  sending = true
  while (chatQueue.length) {
    bot.chat(chatQueue.shift())
    await sleep(1200)
  }
  sending = false
}

// События с кулдауном; data подставляется в реплику J ({have}, {n}, {food}...)
const lastEvent = {}
async function event(type, user = '', cooldownMs = 8000, data = {}) {
  const now = Date.now()
  if (cooldownMs && lastEvent[type] && now - lastEvent[type] < cooldownMs) return
  lastEvent[type] = now
  const res = await post('/event', { type, user, game: 'minecraft', data })
  if (res.reply) say(res.reply)
}

// ======================= инвентарь и инструменты =======================
const MATERIAL_RANK = { wooden: 1, leather: 1, golden: 2, gold: 2, chainmail: 3, stone: 3, iron: 4, diamond: 5, netherite: 6 }
const matRank = (name) => MATERIAL_RANK[name.split('_')[0]] || 0

const LOG_RE = /(^|_)log2?$/
const LEAVES_RE = /(^|_)leaves2?$/
const AIR_RE = /^(air|cave_air|void_air)$/
const NEIGHBORS = [[1, 0, 0], [-1, 0, 0], [0, 1, 0], [0, -1, 0], [0, 0, 1], [0, 0, -1]]

const countItems = (re) =>
  bot.inventory.items().filter((i) => re.test(i.name)).reduce((s, i) => s + i.count, 0)

function bestTool(kind) {
  const re = new RegExp(`_${kind}$`)
  return bot.inventory.items().filter((i) => re.test(i.name)).sort((a, b) => matRank(b.name) - matRank(a.name))[0] || null
}
async function equipTool(kind) {
  const tool = bestTool(kind)
  if (!tool) return false
  if (bot.heldItem && bot.heldItem.name === tool.name) return true
  try { await bot.equip(tool, 'hand'); return true } catch { return false }
}

const ARMOR_PARTS = [
  { re: /_helmet$/, dest: 'head' },
  { re: /_chestplate$/, dest: 'torso' },
  { re: /_leggings$/, dest: 'legs' },
  { re: /_boots$/, dest: 'feet' },
]
// Возвращает { worn: сколько надела, had: была ли броня в инвентаре или на ней }
async function equipArmor() {
  let worn = 0
  let had = false
  for (const part of ARMOR_PARTS) {
    const slot = bot.getEquipmentDestSlot(part.dest)
    const current = bot.inventory.slots[slot]
    const best = bot.inventory.items().filter((i) => part.re.test(i.name)).sort((a, b) => matRank(b.name) - matRank(a.name))[0]
    if (current || best) had = true
    if (best && matRank(best.name) > (current ? matRank(current.name) : 0)) {
      try { await bot.equip(best, part.dest); worn++ } catch { /* слот занят или не получилось — идём дальше */ }
    }
  }
  return { worn, had }
}

// ======================= движение =======================
let movements
function goalTimeout(goal, ms) {
  return Promise.race([
    bot.pathfinder.goto(goal),
    sleep(ms).then(() => { throw new Error('timeout') }),
  ])
}
async function goNear(pos, range = 2, ms = 20000) {
  try {
    await goalTimeout(new goals.GoalNear(pos.x, pos.y, pos.z, range), ms)
    return true
  } catch {
    bot.pathfinder.setGoal(null)
    return false
  }
}

// ======================= очередь задач (планировщик) =======================
const taskQueue = []
let current = null
let running = false

function cancelAll() {
  taskQueue.length = 0
  if (current) current.cancelled = true
  bot.pathfinder.setGoal(null)
  try { bot.stopDigging() } catch { /* ничего не копали */ }
  bot.clearControlStates()
}

function submit(actions, username) {
  const busy = running
  let queued = 0
  for (const a of actions) {
    if (a.type === 'stop') { cancelAll(); continue }            // мгновенно
    if (a.type === 'status') { doStatus(); continue }           // мгновенно
    taskQueue.push({ ...a, user: username, cancelled: false })
    queued++
  }
  if (busy && queued) event('queued', username, 4000)
  runQueue()
}

async function runQueue() {
  if (running) return
  running = true
  while (taskQueue.length) {
    current = taskQueue.shift()
    try {
      await runTask(current)
    } catch (err) {
      console.log('Ошибка задачи', current.type, err.message)
      event('stuck', '', 10000)
    }
    current = null
  }
  running = false
}

async function runTask(t) {
  switch (t.type) {
    case 'follow': return doFollow(t)
    case 'come': return doCome(t)
    case 'chop': return doGather(t, GATHER.chop)
    case 'mine_stone': return doGather(t, GATHER.mine)
    case 'attack': return doAttack(t)
    case 'equip_armor': return doArmor()
    case 'eat': return doEat()
    case 'save_place': return doSavePlace(t)
    case 'goto_place': return doGotoPlace(t)
    case 'list_places': return doListPlaces(t)
    case 'forget_place': return doForgetPlace(t)
    case 'craft': return doCraft(t)
    default: console.log('Неизвестная задача', t.type)
  }
}

// ======================= простые действия =======================
function doFollow(t) {
  const target = bot.players[t.user]?.entity
  if (!target) return event('no_player', t.user, 0)
  bot.pathfinder.setGoal(new goals.GoalFollow(target, 2), true)
}
async function doCome(t) {
  const target = bot.players[t.user]?.entity
  if (!target) return event('no_player', t.user, 0)
  await goNear(target.position, 1)
}
function doStatus() {
  event('status', '', 0, { hp: Math.round(bot.health), food: Math.round(bot.food) })
}
async function doArmor() {
  const { worn, had } = await equipArmor()
  if (worn) return event('armor_done', '', 0, { items: worn })
  return event(had ? 'armor_already' : 'armor_none', '', 0)
}

const FOOD_PREFERENCE = [
  'golden_carrot', 'cooked_beef', 'cooked_porkchop', 'cooked_mutton', 'cooked_salmon', 'cooked_cod', 'cooked_fish',
  'cooked_chicken', 'cooked_rabbit', 'bread', 'baked_potato', 'pumpkin_pie', 'mushroom_stew', 'rabbit_stew',
  'beetroot_soup', 'apple', 'carrot', 'melon_slice', 'melon', 'cookie', 'beef', 'porkchop', 'mutton', 'dried_kelp',
]
function pickFood() {
  const inv = bot.inventory.items()
  for (const name of FOOD_PREFERENCE) {
    const item = inv.find((i) => i.name === name)
    if (item) return item
  }
  return null
}
async function doEat() {
  if (bot.food >= 20) return event('eat_full', '', 0)
  let ate = 0
  while (bot.food < 18 && ate < 4) {
    const food = pickFood()
    if (!food) break
    try {
      await bot.equip(food, 'hand')
      await bot.consume()
      ate++
    } catch { break }
  }
  if (!ate) return event('eat_none', '', 0)
  event('eat_done', '', 0, { food: Math.round(bot.food) })
}

// ======================= добыча: дерево и камень =======================
function nearLeaves(pos) {
  for (let dx = -2; dx <= 2; dx++) for (let dz = -2; dz <= 2; dz++) for (let dy = 0; dy <= 6; dy++) {
    const b = bot.blockAt(pos.offset(dx, dy, dz))
    if (b && LEAVES_RE.test(b.name)) return true
  }
  return false
}
const exposed = (b) => NEIGHBORS.some(([x, y, z]) => {
  const n = bot.blockAt(b.position.offset(x, y, z))
  return n && AIR_RE.test(n.name)
})
const hazardNear = (b) => NEIGHBORS.some(([x, y, z]) => {
  const n = bot.blockAt(b.position.offset(x, y, z))
  return n && /lava/.test(n.name)
}) || /^(gravel|sand)$/.test(bot.blockAt(b.position.offset(0, 1, 0))?.name || '')

const GATHER = {
  chop: {
    prefix: 'chop',
    tool: 'axe',
    needTool: false,
    // только деревья с листвой: не трогаем брёвна в чужих постройках и «обтёсанные» блоки
    match: (b) => LOG_RE.test(b.name) && !b.name.startsWith('stripped'),
    accept: (b) => nearLeaves(b.position),
    items: LOG_RE,
  },
  mine: {
    prefix: 'mine',
    tool: 'pickaxe',
    needTool: true,
    // только открытые блоки без лавы рядом: не копаем туннелей и не лезем в пещеры
    match: (b) => b.name === 'stone' || b.name === 'cobblestone',
    accept: (b) => {
      const me = bot.entity.position.floored()
      const under = b.position.x === me.x && b.position.z === me.z && b.position.y === me.y - 1
      return !under && exposed(b) && !hazardNear(b)
    },
    items: /^cobblestone$/,
  },
}

function findTarget(cfg, bad) {
  const me = bot.entity.position
  const found = bot.findBlocks({ matching: (b) => b && cfg.match(b), maxDistance: 48, count: 96 })
  const candidates = found.filter((p) => !bad.has(keyOf(p))).sort((a, b) => a.distanceTo(me) - b.distanceTo(me))
  for (const p of candidates) {
    const block = bot.blockAt(p)
    if (block && cfg.accept(block)) return block
    bad.add(keyOf(p))
  }
  return null
}

const isDrop = (e) => e && (e.name === 'item' || e.objectType === 'Item' || e.displayName === 'Item')
async function pickupDrops(t) {
  await sleep(450) // дать дропу упасть
  for (let i = 0; i < 4 && !t.cancelled; i++) {
    const drop = bot.nearestEntity((e) => isDrop(e) && e.position.distanceTo(bot.entity.position) < 8)
    if (!drop) return
    await goNear(drop.position, 0.6, 5000)
  }
}

async function doGather(t, cfg) {
  if (cfg.needTool && !bestTool(cfg.tool)) return event(`${cfg.prefix}_no_tool`, '', 0)
  const before = countItems(cfg.items)
  const bad = new Set()
  let gained = 0
  let dug = 0
  let reported = 0
  while (!t.cancelled && gained < t.count) {
    const block = findTarget(cfg, bad)
    if (!block) break
    // подойти на дистанцию копания
    if (!(await goNear(block.position, 3))) { bad.add(keyOf(block.position)); continue }
    if (t.cancelled) break
    const fresh = bot.blockAt(block.position)
    if (!fresh || !cfg.match(fresh) || !bot.canDigBlock(fresh)) { bad.add(keyOf(block.position)); continue }
    await equipTool(cfg.tool)
    try {
      await bot.dig(fresh)
      dug++
    } catch { bad.add(keyOf(block.position)); continue }
    await pickupDrops(t)
    gained = countItems(cfg.items) - before
    if (dug >= 3 && gained === 0) { event('pickup_fail', '', 0); return } // копаем, а в инвентаре пусто
    if (gained - reported >= 4 && gained < t.count) {
      reported = gained
      event(`${cfg.prefix}_progress`, '', 0, { have: gained, n: t.count })
    }
  }
  if (t.cancelled) return
  const end = gained >= t.count ? 'done' : gained > 0 ? 'partial' : 'none'
  event(`${cfg.prefix}_${end}`, '', 0, { have: gained, n: t.count })
}

// ======================= бой =======================
const HOSTILE = new Set([
  'zombie', 'husk', 'drowned', 'zombie_villager', 'skeleton', 'stray', 'spider', 'cave_spider', 'witch', 'slime',
  'pillager', 'vindicator', 'phantom',
]) // криперов и эндерменов в ближний бой не берём — это не трусость, это охрана труда

async function fight(t, target) {
  bot.pathfinder.setGoal(new goals.GoalFollow(target, 2), true)
  const start = Date.now()
  while (!t.cancelled && bot.entities[target.id] && target.isValid !== false && Date.now() - start < 20000) {
    if (bot.health <= 6) break
    if (bot.entity.position.distanceTo(target.position) < 3.2) {
      try { await bot.lookAt(target.position.offset(0, (target.height || 1.6) * 0.8, 0), true) } catch { /* цель пропала */ }
      bot.attack(target)
      await sleep(650) // пауза = перезарядка удара в версиях 1.9+
    } else {
      await sleep(150)
    }
  }
  bot.pathfinder.setGoal(null)
  return !bot.entities[target.id] || target.isValid === false
}

async function doAttack(t) {
  // подготовка: броня и оружие (планировщик сам добавляет шаги перед боем)
  await equipArmor()
  if (!(await equipTool('sword'))) await equipTool('axe')
  const allowed = t.targets && t.targets.length ? new Set(t.targets) : HOSTILE
  let kills = 0
  const started = Date.now()
  while (!t.cancelled && Date.now() - started < 120000) {
    if (bot.health <= 6) { event('attack_retreat', '', 0); break }
    const target = bot.nearestEntity((e) => allowed.has(e.name) && e.position.distanceTo(bot.entity.position) < 24)
    if (!target) break
    if (await fight(t, target)) kills++
  }
  if (t.cancelled) return
  event(kills ? 'attack_done' : 'attack_none', '', 0, { kills })
}

// ======================= память мест (waypoints) =======================
function normalizePlaceKey(name) {
  return String(name || '').trim().toLowerCase().replace(/ё/g, 'е')
}

const PLACE_KEY_ALIASES = {
  home: 'дом',
  домой: 'дом',
  дом: 'home',
  base: 'база',
  база: 'base',
  spawn: 'спавн',
  спавн: 'spawn',
  death: 'смерть',
  смерть: 'death',
}

function loadPlaces() {
  try {
    if (!fs.existsSync(PLACES_FILE)) return {}
    const raw = fs.readFileSync(PLACES_FILE, 'utf8').replace(/^\uFEFF/, '')
    const parsed = JSON.parse(raw || '{}')
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {}
  } catch (err) {
    console.log('[места] ошибка чтения:', err.message)
    return {}
  }
}

function savePlaces(places) {
  const dir = path.dirname(PLACES_FILE)
  fs.mkdirSync(dir, { recursive: true })
  const tmp = `${PLACES_FILE}.tmp`
  const payload = JSON.stringify(places, null, 2)
  fs.writeFileSync(tmp, payload, 'utf8')
  for (let i = 0; i < 3; i++) {
    try {
      fs.renameSync(tmp, PLACES_FILE)
      return
    } catch (err) {
      if (i === 2) {
        fs.writeFileSync(PLACES_FILE, payload, 'utf8')
        try { fs.unlinkSync(tmp) } catch { /* ignore */ }
      }
    }
  }
}

function resolvePlaceEntry(places, rawName) {
  const k = normalizePlaceKey(rawName)
  if (!k) return null
  if (places[k]) return { key: k, entry: places[k] }
  const alias = PLACE_KEY_ALIASES[k]
  if (alias && places[alias]) return { key: alias, entry: places[alias] }
  for (const [pk, val] of Object.entries(places)) {
    if (normalizePlaceKey(val?.name) === k) return { key: pk, entry: val }
  }
  const stem = (s) => (s.length >= 4 && /[аяуеиоы]$/.test(s) ? s.slice(0, -1) : s)
  const kStem = stem(k)
  for (const [pk, val] of Object.entries(places)) {
    const pStem = stem(pk)
    if (pStem === kStem || (pStem.length >= 4 && kStem.length >= 4 && pStem.slice(0, 4) === kStem.slice(0, 4))) {
      return { key: pk, entry: val }
    }
  }
  return null
}

function upsertPlace(rawName, pos, dimension) {
  const displayName = String(rawName || '').trim() || 'дом'
  const k = normalizePlaceKey(displayName)
  const places = loadPlaces()
  const entry = {
    name: displayName,
    x: Math.round(pos.x),
    y: Math.round(pos.y),
    z: Math.round(pos.z),
    dimension: dimension || bot.game?.dimension || 'overworld',
    updatedAt: new Date().toISOString(),
  }
  places[k] = entry
  savePlaces(places)
  return entry
}

async function doSavePlace(t) {
  if (!bot.entity) return
  const entry = upsertPlace(t.place || 'дом', bot.entity.position)
  return event('place_saved', t.user, 0, {
    place: entry.name,
    pos: `${entry.x}, ${entry.y}, ${entry.z}`,
  })
}

async function doListPlaces(t) {
  const entries = Object.values(loadPlaces())
  if (!entries.length) return event('place_empty', t.user, 0)
  return event('place_list', t.user, 0, { places: entries.map((e) => e.name).join(', ') })
}

async function doForgetPlace(t) {
  const places = loadPlaces()
  const found = resolvePlaceEntry(places, t.place)
  if (!found) return event('place_not_found', t.user, 0, { place: t.place || '' })
  delete places[found.key]
  savePlaces(places)
  return event('place_deleted', t.user, 0, { place: found.entry?.name || t.place })
}

async function doGotoPlace(t) {
  const found = resolvePlaceEntry(loadPlaces(), t.place)
  if (!found) return event('place_not_found', t.user, 0, { place: t.place || '' })
  const target = found.entry
  if (target.dimension && bot.game?.dimension && target.dimension !== bot.game.dimension) {
    return event('place_not_found', t.user, 0, { place: target.name })
  }
  const distTo = (a, b) => Math.hypot(a.x - b.x, a.y - b.y, a.z - b.z)
  let stalledSteps = 0
  while (!t.cancelled && bot.entity) {
    const me = bot.entity.position
    const dist = distTo(me, target)
    if (dist <= 3.5) {
      return event('place_reached', t.user, 0, { place: target.name })
    }
    let step = target
    let stepRange = 2
    if (dist > 64) {
      const scale = 48 / dist
      step = {
        x: Math.round(me.x + (target.x - me.x) * scale),
        y: Math.round(me.y + (target.y - me.y) * scale),
        z: Math.round(me.z + (target.z - me.z) * scale),
      }
      stepRange = 6
    }
    const ok = await goNear(step, stepRange, 20000)
    if (t.cancelled || !bot.entity) return
    if (distTo(bot.entity.position, target) <= 3.5) {
      return event('place_reached', t.user, 0, { place: target.name })
    }
    const progressed = distTo(bot.entity.position, target) < dist - 2
    if (!ok && !progressed) {
      stalledSteps++
      if (dist <= 64 || stalledSteps >= 2) {
        return event('stuck', t.user, 5000)
      }
    } else {
      stalledSteps = 0
    }
  }
}

// ======================= крафт =======================
const isPlankName = (name) => name === 'planks' || /_planks$/.test(name || '')
const isLogItemName = (name) => /(^|_)(log2?|stem|hyphae)$/.test(name || '') && !String(name || '').startsWith('stripped_')

const TIER_LABELS = {
  diamond: { m: 'алмазный', f: 'алмазная' },
  iron: { m: 'железный', f: 'железная' },
  stone: { m: 'каменный', f: 'каменная' },
  wooden: { m: 'деревянный', f: 'деревянная' },
  golden: { m: 'золотой', f: 'золотая' },
}
const TOOL_LABELS = {
  pickaxe: ['кирка', 'f'],
  axe: ['топор', 'm'],
  sword: ['меч', 'm'],
  shovel: ['лопата', 'f'],
}

function resolveCraftCandidates(itemKey, fallbackLabel) {
  const itemsByName = bot.registry?.itemsByName || {}
  const out = []
  const pushIfKnown = (name, label) => {
    if (itemsByName[name]) out.push({ name, id: itemsByName[name].id, label })
  }
  if (TOOL_LABELS[itemKey]) {
    const [baseName, g] = TOOL_LABELS[itemKey]
    for (const tier of ['diamond', 'iron', 'stone', 'wooden']) {
      pushIfKnown(`${tier}_${itemKey}`, `${TIER_LABELS[tier][g]} ${baseName}`)
    }
    return out
  }
  if (itemKey === 'planks') {
    const seen = new Set()
    for (const inv of bot.inventory.items()) {
      if (!isLogItemName(inv.name)) continue
      const m = inv.name.match(/^(.+)_(log|stem|hyphae)$/)
      const cand = m ? `${m[1]}_planks` : 'planks'
      if (itemsByName[cand] && !seen.has(cand)) {
        seen.add(cand)
        pushIfKnown(cand, fallbackLabel || 'доски')
      }
    }
    for (const cand of ['oak_planks', 'planks', 'spruce_planks', 'birch_planks', 'jungle_planks', 'acacia_planks', 'dark_oak_planks']) {
      if (itemsByName[cand] && !seen.has(cand)) {
        seen.add(cand)
        pushIfKnown(cand, fallbackLabel || 'доски')
      }
    }
    return out
  }
  if (itemKey === 'bed') {
    const seen = new Set()
    for (const inv of bot.inventory.items()) {
      const m = inv.name.match(/^(.+)_wool$/)
      if (m) {
        const cand = `${m[1]}_bed`
        if (itemsByName[cand] && !seen.has(cand)) {
          seen.add(cand)
          pushIfKnown(cand, fallbackLabel || 'кровать')
        }
      }
    }
    for (const cand of ['bed', 'white_bed', 'red_bed', 'black_bed', 'blue_bed', 'green_bed', 'yellow_bed']) {
      if (itemsByName[cand] && !seen.has(cand)) {
        seen.add(cand)
        pushIfKnown(cand, fallbackLabel || 'кровать')
      }
    }
    return out
  }
  pushIfKnown(itemKey, fallbackLabel || itemKey)
  return out
}

function findNearbyCraftingTable() {
  return bot.findBlock({ matching: (b) => b && b.name === 'crafting_table', maxDistance: 16 })
}

function logMatchesPlank(logItem, plankName, plankMeta) {
  if (plankName === 'planks') {
    if (plankMeta == null) return true
    if (logItem.name === 'log') return logItem.metadata === plankMeta
    if (logItem.name === 'log2') return 4 + logItem.metadata === plankMeta
    return false
  }
  const woodPrefix = plankName.replace(/_planks$/, '')
  return logItem.name === `${woodPrefix}_log` || logItem.name === `${woodPrefix}_stem` || logItem.name === `${woodPrefix}_hyphae`
}

function simulateRecipePlan(recipe, craftCount) {
  const pool = bot.inventory.items().map((it) => ({
    id: it.type,
    name: it.name,
    metadata: it.metadata,
    count: it.count,
  }))
  const consumeExact = (id, meta, need) => {
    let left = need
    for (const p of pool) {
      if (p.id === id && (meta == null || p.metadata === meta) && p.count > 0) {
        const take = Math.min(p.count, left)
        p.count -= take
        left -= take
        if (left === 0) break
      }
    }
    return need - left
  }
  const consumeAnyPlanks = (need) => {
    let left = need
    for (const p of pool) {
      if (isPlankName(p.name) && p.count > 0) {
        const take = Math.min(p.count, left)
        p.count -= take
        left -= take
        if (left === 0) break
      }
    }
    return need - left
  }
  const convertLogsForPlanks = (plankName, plankMeta, needPlanks) => {
    let produced = 0
    for (const p of pool) {
      if (!isLogItemName(p.name) || p.count <= 0) continue
      if (plankName && !logMatchesPlank(p, plankName, plankMeta)) continue
      const logsNeeded = Math.min(p.count, Math.ceil((needPlanks - produced) / 4))
      if (logsNeeded <= 0) continue
      p.count -= logsNeeded
      produced += logsNeeded * 4
      if (produced >= needPlanks) break
    }
    if (produced > needPlanks) {
      // Излишек досок оставляем в пуле как доступные доски
      const plankItem = bot.registry?.itemsByName?.[plankName || 'oak_planks'] || bot.registry?.itemsByName?.planks
      if (plankItem) {
        pool.push({ id: plankItem.id, name: plankItem.name, metadata: plankMeta ?? 0, count: produced - needPlanks })
      }
    }
    return produced
  }

  let plankCraftsNeeded = 0
  let stickCraftsNeeded = 0
  for (const d of recipe.delta || []) {
    if (d.count >= 0) continue
    const need = -d.count * craftCount
    const got = consumeExact(d.id, d.metadata, need)
    let deficit = need - got
    if (deficit <= 0) continue

    const itemName = bot.registry?.items?.[d.id]?.name || ''
    if (itemName === 'stick') {
      const batches = Math.ceil(deficit / 4)
      stickCraftsNeeded += batches
      const planksNeed = batches * 2
      const fromInvPlanks = consumeAnyPlanks(planksNeed)
      const remPlanks = planksNeed - fromInvPlanks
      if (remPlanks > 0) {
        const logBatches = Math.ceil(remPlanks / 4)
        const gotPlanks = convertLogsForPlanks(null, null, remPlanks)
        if (gotPlanks < remPlanks) return null
        plankCraftsNeeded += logBatches
      }
    } else if (isPlankName(itemName)) {
      const gotPlanks = convertLogsForPlanks(itemName, d.metadata, deficit)
      if (gotPlanks < deficit) return null
      plankCraftsNeeded += Math.ceil(deficit / 4)
    } else {
      return null
    }
  }

  let needTableCraft = false
  if (recipe.requiresTable) {
    const nearby = findNearbyCraftingTable()
    const tableItem = bot.registry?.itemsByName?.crafting_table
    const hasTableInv = tableItem ? consumeExact(tableItem.id, null, 1) > 0 : false
    if (!nearby && !hasTableInv) {
      const fromPlanks = consumeAnyPlanks(4)
      const rem = 4 - fromPlanks
      if (rem > 0) {
        const gotPlanks = convertLogsForPlanks(null, null, rem)
        if (gotPlanks < rem) return null
        plankCraftsNeeded += Math.ceil(rem / 4)
      }
      needTableCraft = true
    }
  }

  return { plankCraftsNeeded, stickCraftsNeeded, needTableCraft }
}

async function craftPlanksFromLogs(neededCount, preferredPlankId = null, preferredMeta = null) {
  let crafted = 0
  const itemsByName = bot.registry?.itemsByName || {}
  const prefName = preferredPlankId ? (bot.registry?.items?.[preferredPlankId]?.name || '') : ''
  while (crafted < neededCount) {
    const logItem = bot.inventory.items().find((it) =>
      isLogItemName(it.name) && (!prefName || logMatchesPlank(it, prefName, preferredMeta))
    )
    if (!logItem) break
    let plankId = preferredPlankId
    let plankMeta = preferredMeta
    if (!plankId) {
      if (itemsByName.planks) {
        plankId = itemsByName.planks.id
        plankMeta = logItem.name === 'log2' ? 4 + logItem.metadata : logItem.metadata
      } else {
        const m = logItem.name.match(/^(.+)_(log|stem|hyphae)$/)
        const pName = m ? `${m[1]}_planks` : 'oak_planks'
        plankId = itemsByName[pName]?.id
        plankMeta = null
      }
    }
    if (!plankId) break
    const recipes = bot.recipesFor(plankId, plankMeta, 1, null)
    if (!recipes.length) break
    await bot.craft(recipes[0], 1, null)
    crafted += recipes[0].result?.count || 4
  }
  return crafted
}

async function craftSticksFromPlanks(neededCount) {
  const stickId = bot.registry?.itemsByName?.stick?.id
  if (!stickId) return 0
  let crafted = 0
  while (crafted < neededCount) {
    let recipes = bot.recipesFor(stickId, null, 1, null)
    if (!recipes.length) {
      const gotPlanks = await craftPlanksFromLogs(2)
      if (!gotPlanks) break
      recipes = bot.recipesFor(stickId, null, 1, null)
    }
    if (!recipes.length) break
    await bot.craft(recipes[0], 1, null)
    crafted += recipes[0].result?.count || 4
  }
  return crafted
}

async function placeCraftingTableNearBot() {
  const tableInv = bot.inventory.items().find((i) => i.name === 'crafting_table')
  if (!tableInv || !bot.entity) return null
  const me = bot.entity.position.floored()
  const Vec3 = bot.entity.position.constructor
  const candidates = []
  for (let dx = -3; dx <= 3; dx++) {
    for (let dz = -3; dz <= 3; dz++) {
      for (let dy = -1; dy <= 1; dy++) {
        if (dx === 0 && dz === 0 && (dy === 0 || dy === 1)) continue
        const targetPos = me.offset(dx, dy, dz)
        const dist = bot.entity.position.distanceTo(targetPos)
        if (dist < 1.3 || dist > 4.2) continue
        candidates.push({ pos: targetPos, dist })
      }
    }
  }
  candidates.sort((a, b) => a.dist - b.dist)
  for (const c of candidates) {
    const air = bot.blockAt(c.pos)
    const below = bot.blockAt(c.pos.offset(0, -1, 0))
    if (!air || !below || !AIR_RE.test(air.name) || below.boundingBox !== 'block') continue
    try {
      await bot.equip(tableInv, 'hand')
      await bot.placeBlock(below, new Vec3(0, 1, 0))
      const placed = bot.blockAt(c.pos)
      if (placed && placed.name === 'crafting_table') return placed
    } catch { /* пробуем соседнюю клетку */ }
  }
  return null
}

async function ensureCraftingTable() {
  const existing = findNearbyCraftingTable()
  if (existing) return { block: existing, reason: null }
  const tableId = bot.registry?.itemsByName?.crafting_table?.id
  if (!tableId) return { block: null, reason: 'no_table' }
  if (!bot.inventory.items().some((i) => i.name === 'crafting_table')) {
    let recipes = bot.recipesFor(tableId, null, 1, null)
    if (!recipes.length) {
      await craftPlanksFromLogs(4)
      recipes = bot.recipesFor(tableId, null, 1, null)
    }
    if (!recipes.length) return { block: null, reason: 'no_materials' }
    await bot.craft(recipes[0], 1, null)
  }
  const placed = await placeCraftingTableNearBot()
  if (!placed) return { block: null, reason: 'no_table' }
  return { block: placed, reason: null }
}

async function prepareRecipeMaterials(recipe, craftCount) {
  for (const d of recipe.delta || []) {
    if (d.count >= 0) continue
    const need = -d.count * craftCount
    const have = bot.inventory.count(d.id, d.metadata)
    if (have >= need) continue
    const itemName = bot.registry?.items?.[d.id]?.name || ''
    if (isPlankName(itemName)) {
      await craftPlanksFromLogs(need - have, d.id, d.metadata)
    } else if (itemName === 'stick') {
      await craftSticksFromPlanks(need - have)
    }
  }
}

async function doCraft(t) {
  const wantedCount = Math.max(1, Number(t.count) || 1)
  const candidates = resolveCraftCandidates(t.item, t.label)
  if (!candidates.length) {
    return event('craft_unknown', t.user, 0, { item: t.label || t.item })
  }

  let chosen = null
  let sawRecipe = false
  for (const cand of candidates) {
    const allRecipes = bot.recipesAll(cand.id, null, true)
    if (!allRecipes.length) continue
    sawRecipe = true
    for (const recipe of allRecipes) {
      const craftCount = Math.ceil(wantedCount / (recipe.result?.count || 1))
      const plan = simulateRecipePlan(recipe, craftCount)
      if (plan) {
        chosen = { cand, recipe, craftCount, plan }
        break
      }
    }
    if (chosen) break
  }

  if (!sawRecipe) {
    return event('craft_unknown', t.user, 0, { item: t.label || t.item })
  }
  if (!chosen) {
    return event('craft_no_materials', t.user, 0, { item: t.label || t.item })
  }

  const { cand, recipe, craftCount } = chosen
  try {
    await prepareRecipeMaterials(recipe, craftCount)
    if (t.cancelled) return
    let tableBlock = null
    if (recipe.requiresTable) {
      const tbl = await ensureCraftingTable()
      if (!tbl.block) {
        return event(tbl.reason === 'no_materials' ? 'craft_no_materials' : 'craft_no_table', t.user, 0, { item: cand.label })
      }
      tableBlock = tbl.block
      await goNear(tableBlock.position, 3, 15000)
      if (t.cancelled) return
    }
    const direct = bot.recipesFor(cand.id, null, wantedCount, tableBlock || null)
    const activeRecipe = direct[0] || recipe
    await bot.craft(activeRecipe, craftCount, tableBlock || null)
    const totalOut = craftCount * (activeRecipe.result?.count || 1)
    return event('craft_done', t.user, 0, { item: cand.label, count: totalOut })
  } catch (err) {
    console.log('[крафт]', err.message)
    const missingTable = recipe.requiresTable && !findNearbyCraftingTable()
    return event(missingTable ? 'craft_no_table' : 'craft_no_materials', t.user, 0, { item: cand.label })
  }
}

// ======================= огонь и опасности (приоритетные прерывания) =======================
function seekWater() {
  const water = bot.findBlock({ matching: (b) => b && (b.name === 'water' || b.name === 'flowing_water'), maxDistance: 32 })
  if (!water) return event('no_water', '', 15000)
  const p = water.position
  bot.pathfinder.setGoal(new goals.GoalBlock(p.x, p.y, p.z))
}

bot.once('spawn', () => {
  try {
    movements = new Movements(bot)
    movements.canDig = false              // сама туннели не роет, чужие постройки не ломает
    movements.scafoldingBlocks = []       // блоки под ноги не ставит (в библиотеке именно так написано)
    movements.scaffoldingBlocks = []
    movements.allow1by1towers = false
    bot.pathfinder.setMovements(movements)
    bot.pathfinder.thinkTimeout = 4000
  } catch (err) {
    console.log('pathfinder:', err.message)
  }
  event('spawn', '', 0)
  if (SKIN) {
    setTimeout(() => {
      try {
        bot.chat('/skin ' + SKIN)
      } catch (err) {
        console.log('[скин]', err.message)
      }
    }, 2500)
  }
  if (bot.entity) {
    try {
      const places = loadPlaces()
      if (!places['спавн']) upsertPlace('спавн', bot.entity.position)
    } catch (err) {
      console.log('[места]', err.message)
    }
  }

  let wasBurning = false
  let wasDay = bot.time?.isDay ?? true
  setInterval(() => {
    if (!bot.entity) return
    // бит 0x01 в первом поле метаданных сущности = «горит»
    const burning = (Number(bot.entity.metadata?.[0] ?? 0) & 0x01) === 1
    if (burning && !wasBurning) {
      cancelAll()           // огонь важнее любой задачи
      event('burning', '', 5000)
      seekWater()
    } else if (!burning && wasBurning) {
      event('extinguished', '', 5000)
    }
    wasBurning = burning

    const isDay = bot.time?.isDay ?? true
    if (wasDay && !isDay) event('night', '', 60000)
    wasDay = isDay
  }, 500)

  setInterval(() => {
    if (!bot.entity) return
    const creeper = bot.nearestEntity((e) => e.name === 'creeper' && bot.entity.position.distanceTo(e.position) < 7)
    if (creeper) {
      event('creeper', '', 30000)
      const gathering = current && (current.type === 'chop' || current.type === 'mine_stone')
      if (gathering && bot.entity.position.distanceTo(creeper.position) < 5) {
        cancelAll()
        event('danger_abort', '', 15000)
      }
    }
    // низкое здоровье: прервать добычу, потом поесть, если есть чем
    const gathering = current && (current.type === 'chop' || current.type === 'mine_stone')
    if (gathering && bot.health <= 6) {
      cancelAll()
      event('danger_abort', '', 15000)
      if (bot.food < 20 && pickFood()) submit([{ type: 'eat' }], '')
    }
  }, 1000)
})

bot.on('health', () => {
  if (bot.health > 0 && bot.health <= 6) event('low_health', '', 30000)
})
bot.on('death', () => {
  if (bot.entity?.position) {
    try { upsertPlace('смерть', bot.entity.position) } catch (err) { console.log('[места]', err.message) }
  }
  cancelAll()
  event('death', '', 3000)
})

// ======================= чат =======================
bot.on('chat', async (username, message) => {
  if (username === bot.username) return
  const m = message.match(/^\s*(?:j|дж|джей)(?:[\s,:!]+(.*))?$/i)
  if (!m) return
  const text = (m[1] || '').trim()

  const res = await post('/chat', { user: username, text, game: 'minecraft' })
  const actions = Array.isArray(res.actions) ? res.actions : []
  if (actions.length) {
    if (OWNER && username !== OWNER) return say('Приказы отдаёт владелец.')
    if (res.reply) say(res.reply)
    return submit(actions, username)
  }
  if (res.reply) say(res.reply)
})

bot.on('kicked', (reason) => console.log('Кикнут:', reason))
bot.on('error', (err) => console.log('Ошибка:', err.message))
bot.on('end', () => {
  console.log('Соединение закрыто.')
  process.exit(1)
})
