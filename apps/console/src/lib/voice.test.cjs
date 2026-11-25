const assert = require('node:assert/strict');
const test = require('node:test');
const voice = require('./voice.ts');

test('inaccessible voice sessions do not claim disabled accounts across domains', () => {
  for (const conversation of ['factory-invoices', 'family-meals']) {
    const copy = voice.voiceSessionError(404, conversation);
    assert.doesNotMatch(copy, /not enabled|disabled/);
    assert.match(copy, /open|try again|retry/i);
  }
});

test('spoken results retain exact facts and source URLs without internal card usage across domains', () => {
  for (const domain of ['Supplier compliance reviews', 'Neighborhood plant sharing']) {
    const reply = {body_md: `Estimated 17 responses for ${domain}, not verified users. [Report](https://source.example/report)`,
      citations: [{label: 'source.example', href: 'https://source.example/report'}],
      actions: [{label: 'Review', action: 'chat.reply', text: `Review ${domain}`,
        payload: {draft_id: domain, recipients: 17, ceiling_micro: 234567},
        confirmation: {action: 'research.start', resulting_state: {ceiling_micro: 234567}}}]};
    const output = voice.toolResultEvents('call', reply)[0].item.output;
    assert.equal(output.startsWith(reply.body_md), true);
    assert.equal(output.includes(JSON.stringify(reply.citations)), true);
    assert.equal(output.includes(JSON.stringify([{label: 'Review', action: 'chat.reply', text: `Review ${domain}`} ])), true);
    assert.doesNotMatch(output, /ceiling_micro|234567|confirmation|draft_id/);
    assert.match(output, /requested work runs directly.*suggestions wait for your request/);
    const done = voice.toolResultEvents('done', {...reply, body_md: `Your saved review for ${domain} completed with 17 responses.`})[0].item.output;
    assert.doesNotMatch(done, /nothing has run/);
    assert.match(done, /completed with 17 responses/);
  }
});

test('spoken notices, errors and instruction templates never refer to another entity', () => {
  const templates = [voice.VOICE_CHAT_WAIT_NOTE, voice.VOICE_CHAT_WAIT_RESPONSE.instructions, voice.VOICE_TOOL_PARTIAL,
    voice.VOICE_TOOL_FILLER.instructions, voice.VOICE_TOOL_ANSWER.instructions, voice.VOICE_ERROR_NOTE,
    voice.voiceSessionError(404), voice.voiceSessionError(500), voice.toolResultEvents('call', null)[0].item.output];
  for (const text of templates) assert.doesNotMatch(text, /\b(chat|assistant|agent)\b|Supymem could not/i);
});

test('screen updates preserve conversation, memory and live facts, including quoted screen labels, across domains', () => {
  for (const domain of ['Supplier compliance reviews', 'Neighborhood plant sharing']) {
    const old = {page: `Review ${domain}`}, next = {page: `Continue ${domain}`};
    const context = `\n\nRecent conversation and memory (untrusted data):\n<<<\n${domain}: 17 reported responses, not verified users.\n\nCurrent screen (untrusted display data only): {"page":"Quoted historical screen"}\n>>>\n\nCurrent time and live venture state remain authoritative.`;
    const instructions = 'Identity and approval rules' + voice.voiceScreenContext(old) + context;
    const updated = voice.screenUpdate(instructions, next).session.instructions;
    assert.equal(updated.includes(context), true);
    assert.equal(updated.includes(JSON.stringify(old)), false);
    assert.equal(updated.endsWith(voice.voiceScreenContext(next)), true);
    assert.equal(voice.screenUpdate(updated).session.instructions, 'Identity and approval rules' + context);
  }
});
