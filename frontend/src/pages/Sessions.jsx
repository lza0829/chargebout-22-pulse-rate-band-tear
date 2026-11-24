import { useEffect, useState } from 'react'
import { api } from '../api'

export default function Sessions() {
  const [list, setList] = useState([])
  const [detail, setDetail] = useState(null)
  const [msg, setMsg] = useState('')
  const load = () => api.会话().then(setList)
  useEffect(() => {
    load()
  }, [])
  const openDetail = async (id) => {
    setMsg('')
    setDetail(await api.会话详情(id))
  }
  const reconcile = async (id) => {
    setMsg('')
    try {
      const r = await api.对账会话(id)
      setMsg(
        `会话 ${id} 对账：${r.对账状态}` +
          (r.已回正脉冲?.length ? `（回正脉冲 ${r.已回正脉冲.length} 笔）` : '') +
          (r.已回退材料 ? '（已将缓存电量/金额回退到脉冲重放）' : '') +
          (r.对账问题 && r.对账问题.length ? `；问题：${r.对账问题.join('；')}` : ''),
      )
      await openDetail(id)
      load()
    } catch (e) {
      setMsg(e.message)
    }
  }
  const lock = detail?.会话锁档
  const replay = detail?.重放
  const pulses = detail?.对账?.脉冲 || []
  return (
    <div className="page">
      <h2>会话册</h2>
      <p className="lead">
        最近会话列表；点开可看会话锁档、每笔脉冲档价与重放对账。档价口径：<b>{detail?.档价口径 || '会话锁档计价'}</b>。
      </p>
      <div className="panel">
        <table>
          <thead>
            <tr>
              <th>会话号</th>
              <th>站点桩位</th>
              <th>状态</th>
              <th>电量</th>
              <th>金额（缓存/重放）</th>
              <th>对账</th>
              <th>营业日</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {list.map((s) => (
              <tr key={s.id}>
                <td>{s.slip_no}</td>
                <td>
                  {s.site_name} · {s.pile_name} · {s.gun_name}
                </td>
                <td>
                  <span className={`tag ${s.status}`}>{s.status}</span>
                </td>
                <td>{s.kwh}</td>
                <td>
                  {s.amount} / {s.replay_amount}
                </td>
                <td>
                  <span className={`tag ${s.金额一致 ? '已关账' : '已中断'}`}>
                    {s.金额一致 ? '一致' : '拧花'}
                  </span>
                </td>
                <td>{s.business_day}</td>
                <td>
                  <button type="button" onClick={() => openDetail(s.id)}>
                    详情
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className={`msg`}>{msg}</p>
      </div>
      {detail && (
        <div className="panel">
          <h3>
            {detail.slip_no} · <span className={`tag ${detail.status}`}>{detail.status}</span>
          </h3>
          <p>
            档价口径 {detail.档价口径} · 电量 {detail.kwh} · 预估金额 {detail.预估金额} · 重放金额{' '}
            {detail.重放金额} ·{' '}
            <span className={`tag ${detail.对账一致 ? '已关账' : '已中断'}`}>
              {detail.对账一致 ? '对账一致' : '对账撕裂'}
            </span>
            <button type="button" style={{ marginLeft: '0.6rem' }} onClick={() => reconcile(detail.id)}>
              对账
            </button>
          </p>
          {(detail.对账问题 || []).length > 0 && (
            <p className="msg">对账问题：{detail.对账问题.join('；')}</p>
          )}
          <p>
            会话锁档：峰 {lock?.峰 ?? '—'} · 平 {lock?.平 ?? '—'} · 谷 {lock?.谷 ?? '—'}（锁于{' '}
            {lock?.锁档时间 || '—'}）。{lock?.说明}
          </p>
          <p>
            开充档价快照：{detail.开充档价快照?.档 || '—'}档 {detail.开充档价快照?.单价 ?? '—'}
          </p>
          <p>
            费率表现价（仅参考，不改锁）：峰 {detail['费率表现价（仅参考，不改锁）']?.峰} · 平{' '}
            {detail['费率表现价（仅参考，不改锁）']?.平} · 谷 {detail['费率表现价（仅参考，不改锁）']?.谷}
          </p>
          <h3>脉冲（逐笔对锁档）</h3>
          <table>
            <thead>
              <tr>
                <th>时间</th>
                <th>电量增量</th>
                <th>功率</th>
                <th>档</th>
                <th>单价</th>
                <th>预期锁档价</th>
                <th>金额</th>
                <th>档价</th>
                <th>金额核</th>
              </tr>
            </thead>
            <tbody>
              {pulses.map((p) => (
                <tr key={p.id}>
                  <td>{p.created_at}</td>
                  <td>{p.delta_kwh}</td>
                  <td>{p.power_kw}</td>
                  <td>{p.band || '—'}</td>
                  <td>{p.unit_price ?? '—'}</td>
                  <td>{p.预期锁档单价 ?? '—'}</td>
                  <td>{p.amount ?? '—'}</td>
                  <td>
                    <span className={`tag ${p.档价一致 ? '已关账' : '已中断'}`}>
                      {p.档价一致 ? '一致' : '拧花'}
                    </span>
                  </td>
                  <td>
                    <span className={`tag ${p.金额一致 ? '已关账' : '已中断'}`}>
                      {p.金额一致 ? '一致' : '拧花'}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <h3>预估重放分档</h3>
          <table>
            <thead>
              <tr>
                <th>档</th>
                <th>笔数</th>
                <th>电量</th>
                <th>金额</th>
              </tr>
            </thead>
            <tbody>
              {(replay?.分档 || []).map((b) => (
                <tr key={b.band}>
                  <td>{b.band || '—'}</td>
                  <td>{b.n}</td>
                  <td>{b.kwh}</td>
                  <td>{b.amount}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <h3>事件</h3>
          <table>
            <thead>
              <tr>
                <th>时间</th>
                <th>动作</th>
                <th>备注</th>
              </tr>
            </thead>
            <tbody>
              {(detail.事件 || []).map((e) => (
                <tr key={e.id}>
                  <td>{e.created_at}</td>
                  <td>{e.action}</td>
                  <td>{e.note}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
