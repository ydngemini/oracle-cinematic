export default function ServicePanel({ ec2, rds, lambda, onSelect, selectedService }) {
  const ec2Running = ec2.instances.filter(i => i.state === 'running').length;
  const rdsAvailable = rds.instances.filter(i => i.status === 'available').length;
  
  return (
    <div className="panel">
      <div className="panel-header">
        <h2>Services</h2>
        <span className="panel-count">{ec2.count + rds.count + (lambda?.count || 0)}</span>
      </div>
      <div className="service-list">
        <div className="service-section-label">
          EC2 INSTANCES — {ec2Running}/{ec2.count}
        </div>
        {ec2.instances.slice(0, 20).map(inst => (
          <button
            type="button"
            key={inst.id}
            className={`service-item ${selectedService?.type === 'ec2' && selectedService?.id === inst.id ? 'selected' : ''}`}
            onClick={() => onSelect('ec2', inst.id)}
            aria-pressed={selectedService?.type === 'ec2' && selectedService?.id === inst.id}
          >
            <span className="service-header">
              <span className="service-name">{inst.name || inst.id.slice(0, 12)}</span>
              <span className={`service-state ${inst.state}`}>{inst.state}</span>
            </span>
            <span className="service-meta">{inst.type} · {inst.az}</span>
          </button>
        ))}
        
        <div className="service-section-label" style={{ marginTop: '12px' }}>
          RDS DATABASES — {rdsAvailable}/{rds.count}
        </div>
        {rds.instances.slice(0, 10).map(db => (
          <button
            type="button"
            key={db.id}
            className={`service-item ${selectedService?.type === 'rds' && selectedService?.id === db.id ? 'selected' : ''}`}
            onClick={() => onSelect('rds', db.id)}
            aria-pressed={selectedService?.type === 'rds' && selectedService?.id === db.id}
          >
            <span className="service-header">
              <span className="service-name">{db.id}</span>
              <span className={`service-state ${db.status === 'available' ? 'running' : 'pending'}`}>
                {db.status}
              </span>
            </span>
            <span className="service-meta">{db.engine} {db.engine_version} · {db.class}</span>
          </button>
        ))}
        
        {lambda?.functions?.length > 0 && (
          <>
            <div className="service-section-label" style={{ marginTop: '12px' }}>
              LAMBDA — {lambda.functions.filter(f => f.state === 'Active').length}/{lambda.count}
            </div>
            {lambda.functions.slice(0, 10).map(fn => (
              <button
                type="button"
                key={fn.name}
                className={`service-item ${selectedService?.type === 'lambda' && selectedService?.id === fn.name ? 'selected' : ''}`}
                onClick={() => onSelect('lambda', fn.name)}
                aria-pressed={selectedService?.type === 'lambda' && selectedService?.id === fn.name}
              >
                <span className="service-header">
                  <span className="service-name">{fn.name}</span>
                  <span className={`service-state ${fn.state === 'Active' ? 'running' : 'pending'}`}>
                    {fn.state || 'Active'}
                  </span>
                </span>
                <span className="service-meta">{fn.runtime} · {fn.memory}MB</span>
              </button>
            ))}
          </>
        )}
      </div>
    </div>
  );
}
