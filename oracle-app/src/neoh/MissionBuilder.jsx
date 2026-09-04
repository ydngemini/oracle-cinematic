import { useCallback, useEffect, useState } from 'react';

import { crmGet, crmPost } from '../state/useCrmApi';
import {
  AUTOPILOT_CHANNELS,
  CHANNELS,
  CONSENT_SENTENCES,
  OBJECTIVES,
  VOICE_CAVEAT,
  statusLine,
  toRequest,
  validate,
} from './missionModel';
import styles from './MissionBuilder.module.css';

/**
 * MissionBuilder — say what you want to happen; Neoh works out the work.
 *
 * Three decisions this screen makes deliberately:
 *
 * 1. **The consent sentence is visible before the box is ticked, not after.**
 *    It sits under each autopilot checkbox in full. An agent authorising
 *    software to text their clients under their licence should read the
 *    sentence that will be stored, at the moment they agree to it.
 *
 * 2. **Simulate is not optional.** Live is disabled until a simulation has
 *    run, matching the database CHECK. The button says why rather than
 *    sitting grey.
 *
 * 3. **Shadow is the default.** The first launch records what it would have
 *    done. Nothing about that is a lesser mode — it is the same code path with
 *    the last step withheld.
 */

const EMPTY = {
  objectiveKind: 'listings_won',
  objectiveText: '',
  targetCount: '',
  deadline: '',
  budgetDollars: '',
  allowedChannels: ['sms'],
  autoChannels: [],
};

export function MissionBuilder({ onOpenEntity }) {
  const [draft, setDraft] = useState(EMPTY);
  const [missions, setMissions] = useState([]);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState('');
  const [simulation, setSimulation] = useState(null);
  const [current, setCurrent] = useState(null);

  const load = useCallback(() => {
    crmGet('/api/missions').then(
      (data) => setMissions(data?.missions || []),
      () => setMissions([]),
    );
  }, []);

  useEffect(() => {
    const frame = window.requestAnimationFrame(load);
    return () => window.cancelAnimationFrame(frame);
  }, [load]);

  const toggle = (key, value) => setDraft((prev) => {
    const list = prev[key] || [];
    const next = list.includes(value)
      ? list.filter((v) => v !== value)
      : [...list, value];
    // Removing a channel removes its grant with it — a grant on a channel the
    // mission cannot use would be refused by the API and the database anyway.
    const autoChannels = key === 'allowedChannels'
      ? (prev.autoChannels || []).filter((c) => next.includes(c))
      : prev.autoChannels;
    return { ...prev, [key]: next, autoChannels };
  });

  const check = validate(draft);

  const create = async () => {
    if (!check.ok) { setNotice(check.problem); return; }
    setBusy(true);
    setNotice('');
    try {
      const data = await crmPost('/api/missions', toRequest(draft));
      setCurrent(data.mission);
      setSimulation(null);
      setDraft(EMPTY);
      load();
    } catch (error) {
      setNotice(error?.message || 'The mission could not be created.');
    } finally {
      setBusy(false);
    }
  };

  const simulate = async (mission) => {
    setBusy(true);
    setNotice('');
    try {
      const data = await crmPost(`/api/missions/${mission.id}/simulate`, {});
      setSimulation(data.simulation);
      setCurrent({ ...mission, simulated_at: new Date().toISOString() });
      load();
    } catch (error) {
      setNotice(error?.message || 'The simulation failed.');
    } finally {
      setBusy(false);
    }
  };

  const launch = async (mission, mode) => {
    setBusy(true);
    setNotice('');
    try {
      const data = await crmPost(`/api/missions/${mission.id}/launch`, { mode });
      setCurrent(data.mission);
      load();
    } catch (error) {
      // The API names which credential is missing; show that, not "failed".
      setNotice(error?.message || 'The mission could not be launched.');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className={styles.work}>
      <header className={styles.head}>
        <h1 className={styles.title}>Missions</h1>
        <p className={styles.blurb}>
          Say what you want to happen. Neoh works out who to contact, on which
          channel, and when — and shows you the whole plan before any of it runs.
        </p>
      </header>

      <section className={styles.builder} aria-label="New mission">
        <label className={styles.field}>
          <span className={styles.label}>What do you want to happen?</span>
          <textarea
            className={styles.textarea}
            rows={2}
            maxLength={1000}
            value={draft.objectiveText}
            placeholder="Win three listings in Newark before the end of the quarter"
            onChange={(e) => setDraft({ ...draft, objectiveText: e.target.value })}
          />
        </label>

        <div className={styles.row}>
          <label className={styles.field}>
            <span className={styles.label}>Kind</span>
            <select
              className={styles.select}
              value={draft.objectiveKind}
              onChange={(e) => setDraft({ ...draft, objectiveKind: e.target.value })}
            >
              {OBJECTIVES.map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}
            </select>
          </label>
          <label className={styles.field}>
            <span className={styles.label}>How many</span>
            <input
              className={styles.input} type="number" min="1" inputMode="numeric"
              value={draft.targetCount} placeholder="3"
              onChange={(e) => setDraft({ ...draft, targetCount: e.target.value })}
            />
          </label>
          <label className={styles.field}>
            <span className={styles.label}>By when</span>
            <input
              className={styles.input} type="date" value={draft.deadline}
              onChange={(e) => setDraft({ ...draft, deadline: e.target.value })}
            />
          </label>
          <label className={styles.field}>
            <span className={styles.label}>Budget</span>
            <input
              className={styles.input} type="number" min="0" step="1" inputMode="decimal"
              value={draft.budgetDollars} placeholder="$"
              onChange={(e) => setDraft({ ...draft, budgetDollars: e.target.value })}
            />
          </label>
        </div>

        <fieldset className={styles.fieldset}>
          <legend className={styles.label}>How may Neoh reach people?</legend>
          <div className={styles.chips}>
            {CHANNELS.map((channel) => (
              <label key={channel.id} className={styles.chip}>
                <input
                  type="checkbox"
                  checked={draft.allowedChannels.includes(channel.id)}
                  onChange={() => toggle('allowedChannels', channel.id)}
                />
                {channel.label}
              </label>
            ))}
          </div>
        </fieldset>

        <fieldset className={styles.fieldset}>
          <legend className={styles.label}>What may go without your approval?</legend>
          <p className={styles.hint}>
            Anything you leave unticked still gets prepared — it waits in your
            approval queue instead of going out.
          </p>
          {AUTOPILOT_CHANNELS.filter((c) => draft.allowedChannels.includes(c)).map((id) => (
            <label key={id} className={styles.consent}>
              <span className={styles.consentTop}>
                <input
                  type="checkbox"
                  checked={draft.autoChannels.includes(id)}
                  onChange={() => toggle('autoChannels', id)}
                />
                <strong>{CHANNELS.find((c) => c.id === id)?.label} without asking me</strong>
              </span>
              {/* The stored sentence, shown in full at the moment of agreeing. */}
              <span className={styles.consentText}>{CONSENT_SENTENCES[id]}</span>
              {id === 'voice' && <span className={styles.caveat}>{VOICE_CAVEAT}</span>}
            </label>
          ))}
          {!AUTOPILOT_CHANNELS.some((c) => draft.allowedChannels.includes(c)) && (
            <p className={styles.hint}>
              Choose a way to reach people above and the autopilot options appear here.
            </p>
          )}
        </fieldset>

        {notice && <p className={styles.notice} role="alert">{notice}</p>}

        <button type="button" className={styles.primary} onClick={create} disabled={busy}>
          {busy ? 'Working…' : 'Build this mission'}
        </button>
      </section>

      {simulation && <Simulation simulation={simulation} />}

      {current && (
        <Launcher
          mission={current} busy={busy}
          onSimulate={() => simulate(current)}
          onLaunch={(mode) => launch(current, mode)}
        />
      )}

      <MissionList missions={missions} onSelect={setCurrent} onOpenEntity={onOpenEntity} />
    </div>
  );
}

function Simulation({ simulation }) {
  const { candidates, actions, cost, expected, caveat } = simulation;
  return (
    <section className={styles.simulation} aria-label="Simulation">
      <h2 className={styles.sectionTitle}>What this would do</h2>
      <dl className={styles.stats}>
        <div><dt>People</dt><dd>{candidates.analysed}</dd></div>
        <div><dt>Worth contacting</dt><dd>{candidates.strong}</dd></div>
        <div><dt>Actions</dt><dd>{actions.planned}</dd></div>
        <div><dt>Cost</dt><dd>${(cost.total_cents / 100).toFixed(2)}</dd></div>
        <div><dt>Your time</dt><dd>{cost.total_minutes} min</dd></div>
      </dl>
      <p className={styles.range}>
        Somewhere between <strong>{expected.replies_low}</strong> and{' '}
        <strong>{expected.replies_high}</strong> replies.
      </p>
      {/* The caveat is the point of this panel, not a footnote on it. */}
      <p className={styles.caveat}>{caveat}</p>
      {!cost.within_budget && (
        <p className={styles.notice}>
          This plan costs more than the budget you set. Nothing has been trimmed —
          raise the budget or narrow the mission.
        </p>
      )}
    </section>
  );
}

function Launcher({ mission, busy, onSimulate, onLaunch }) {
  const simulated = Boolean(mission.simulated_at);
  return (
    <section className={styles.launcher} aria-label="Launch">
      <p className={styles.status}>{statusLine(mission)}</p>
      <div className={styles.actions}>
        <button type="button" className={styles.secondary} onClick={onSimulate} disabled={busy}>
          {simulated ? 'Simulate again' : 'Simulate'}
        </button>
        <button type="button" className={styles.secondary} onClick={() => onLaunch('shadow')} disabled={busy}>
          Start in shadow
        </button>
        <button
          type="button" className={styles.primary}
          onClick={() => onLaunch('live')} disabled={busy || !simulated}
          title={simulated ? undefined : 'Simulate it first'}
        >
          Go live
        </button>
      </div>
      {!simulated && (
        <p className={styles.hint}>
          Going live needs a simulation first — nobody should point this at their
          database without seeing what it would do.
        </p>
      )}
    </section>
  );
}

function MissionList({ missions, onSelect }) {
  if (!missions.length) return null;
  return (
    <section className={styles.list} aria-label="Missions">
      <h2 className={styles.sectionTitle}>Your missions</h2>
      <ul className={styles.items}>
        {missions.map((mission) => (
          <li key={mission.id}>
            <button type="button" className={styles.item} onClick={() => onSelect(mission)}>
              <span className={styles.itemName}>{mission.objective_text}</span>
              <span className={styles.itemStatus}>{statusLine(mission)}</span>
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}

export default MissionBuilder;
