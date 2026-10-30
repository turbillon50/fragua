// Compatibility entrypoint: reviewer configured independently of Claude.
import('./review.mjs').then(({main}) => main()).catch(() => { console.error(JSON.stringify({result:'ERROR',error:'REVIEW_EXECUTION_ERROR',approved:false})); process.exitCode=2; });
