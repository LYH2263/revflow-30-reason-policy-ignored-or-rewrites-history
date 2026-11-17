<script setup>
import { computed, onMounted, ref } from 'vue'
import { getJson, postJson } from '../api'

const rows = ref([])
const err = ref('')
const msg = ref('')
const busyId = ref(null)
const confirmNote = ref({})
const confirmBy = ref('')
const selected = ref(null)
const proofs = ref([])

async function load() {
  rows.value = await getJson('/api/quarantine')
  if (selected.value) {
    const still = rows.value.find((r) => r.id === selected.value)
    if (still) await loadProofs(still)
  }
}

async function loadProofs(q) {
  selected.value = q.id
  proofs.value = (await getJson(`/api/quarantine/${q.id}/proofs`)).证明
}

onMounted(async () => {
  try {
    await load()
  } catch (e) {
    err.value = String(e.message || e)
  }
})

const buckets = computed(() => {
  const m = new Map()
  for (const q of rows.value) {
    const k = q.reason_code
    const b = m.get(k) || {
      code: k,
      label: q.reason_label,
      policy: q.policy,
      version: q.policy_version,
      open: 0,
      amount: 0,
    }
    if (q.status === '未平') {
      b.open += 1
      b.amount += q.amount_cents
    }
    m.set(k, b)
  }
  return [...m.values()]
})

async function confirm(q) {
  busyId.value = q.id
  err.value = ''
  msg.value = ''
  try {
    await postJson(`/api/quarantine/${q.id}/confirmation`, {
      confirmed_by: confirmBy.value || '未署名',
      note: confirmNote.value[q.id] || '',
    })
    msg.value = `隔离 #${q.id} 已登记确认标记`
    await load()
    if (selected.value === q.id) await loadProofs(q)
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busyId.value = null
  }
}

function yuan(cents) {
  return (Number(cents || 0) / 100).toFixed(2)
}

function policyClass(p) {
  if (p === '自动可再配') return 'on'
  if (p === '永久拒绝') return 'bad'
  return 'warn'
}

function actionClass(a) {
  if (a === '再配') return 'on'
  if (a === '永久拒绝拦下') return 'bad'
  if (a === '待确认拦下') return 'warn'
  return ''
}
</script>

<template>
  <div>
    <h1>未平隔离</h1>
    <p class="lead">
      每条隔离落有限原因码，并钉死「入隔离时」的策略快照；「当前策略」来自策略表最新版本，
      仅决定下一轮新决策。改策略不会回溯改写历史处置结果。
    </p>
    <p v-if="err" class="msg">{{ err }}</p>
    <p v-if="msg" class="ok">{{ msg }}</p>

    <div class="panel">
      <h2>按原因码分桶</h2>
      <table>
        <thead>
          <tr>
            <th>原因码</th><th>名称</th><th>当前策略</th><th>版本</th>
            <th>未平笔数</th><th>未平金额（元）</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="b in buckets" :key="b.code">
            <td class="mono">{{ b.code }}</td>
            <td>{{ b.label }}</td>
            <td><span class="tag" :class="policyClass(b.policy)">{{ b.policy }}</span></td>
            <td class="mono">v{{ b.version }}</td>
            <td>{{ b.open }}</td>
            <td>{{ yuan(b.amount) }}</td>
          </tr>
          <tr v-if="!buckets.length"><td colspan="6">暂无隔离项（先跑归并）</td></tr>
        </tbody>
      </table>
    </div>

    <div class="panel">
      <h2>隔离明细</h2>
      <div class="row-actions">
        <label class="inline-field">
          确认人
          <input v-model="confirmBy" placeholder="操作人" />
        </label>
      </div>
      <table>
        <thead>
          <tr>
            <th>流水键</th><th>账户</th><th>原因码</th><th>当前策略</th>
            <th>入隔离快照</th><th>金额（元）</th>
            <th>状态</th><th>确认标记</th><th>归并#</th><th>时间</th><th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="q in rows" :key="q.id">
            <td class="mono">{{ q.entry_key }}</td>
            <td>{{ q.account_code }}</td>
            <td>
              <span class="tag warn">{{ q.reason_code }}</span>
              <span class="hint-text">{{ q.reason_label }}</span>
            </td>
            <td>
              <span class="tag" :class="policyClass(q.policy)">{{ q.policy }}</span>
              <span class="hint-text">v{{ q.policy_version }}</span>
            </td>
            <td>
              <span class="tag" :class="policyClass(q.policy_at_quarantine)">
                {{ q.policy_at_quarantine }}
              </span>
              <span class="hint-text">v{{ q.snapshot_version }}</span>
            </td>
            <td>{{ yuan(q.amount_cents) }}</td>
            <td>
              <span class="tag" :class="{ on: q.status === '已再配', bad: q.status === '未平' }">
                {{ q.status }}
              </span>
            </td>
            <td>
              <template v-if="q.confirmation">
                <span class="tag on">已确认</span>
                <span class="hint-text">
                  {{ q.confirmation.confirmed_by }} / {{ q.confirmation.policy_at_confirm }}
                  v{{ q.confirmation.policy_version }}
                </span>
              </template>
              <template v-else-if="q.can_confirm">
                <input v-model="confirmNote[q.id]" placeholder="确认备注" class="note-input" />
                <button class="btn secondary btn-sm" :disabled="busyId === q.id" @click="confirm(q)">
                  登记确认
                </button>
              </template>
              <span v-else class="hint-text">—</span>
            </td>
            <td>{{ q.settle_run_id }}</td>
            <td class="mono">{{ q.created_at }}</td>
            <td>
              <button class="btn secondary btn-sm" @click="loadProofs(q)">证明</button>
            </td>
          </tr>
          <tr v-if="!rows.length"><td colspan="11">暂无隔离项</td></tr>
        </tbody>
      </table>
    </div>

    <div class="panel" v-if="selected">
      <h2>隔离 #{{ selected }} · 策略证明链（只读）</h2>
      <table>
        <thead>
          <tr>
            <th>证明键</th><th>隔离键</th><th>原因码</th><th>策略版本</th>
            <th>策略</th><th>动作</th><th>归并#</th><th>说明</th><th>时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="p in proofs" :key="p.proof_key">
            <td class="mono">{{ p.proof_key }}</td>
            <td class="mono">{{ p.quarantine_key }}</td>
            <td class="mono">{{ p.reason_code }}</td>
            <td class="mono">v{{ p.policy_version }}</td>
            <td><span class="tag" :class="policyClass(p.policy)">{{ p.policy }}</span></td>
            <td><span class="tag" :class="actionClass(p.action)">{{ p.action }}</span></td>
            <td>{{ p.settle_run_id ?? '—' }}</td>
            <td class="desc">{{ p.detail }}</td>
            <td class="mono">{{ p.created_at }}</td>
          </tr>
          <tr v-if="!proofs.length"><td colspan="9">该隔离尚无证明</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
