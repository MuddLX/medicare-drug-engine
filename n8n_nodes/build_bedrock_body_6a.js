// Node 6a - Build Bedrock Body (2026-10-08)
// Packs the downloaded file + the reading instructions into one request for Claude on AWS Bedrock.
// Same pattern as Build Railway Body: build the whole body here, stringify it, and the HTTP node sends it as-is.
// PDFs go as a document; screenshots (PNG/JPG/GIF/WEBP) go as an image. Anything else stops with a clear message.

const PROMPT = `CRITICAL: Return only a valid JSON object. No explanation, no markdown, no code blocks. Start your response with { and end with }.
You are reading client information for a Medicare insurance agency. Usually it is the agency's own Client Information Sheet (an intake form where many fields are optional). Sometimes it is instead a screenshot of an email or text message from a client, or pasted email text, that lists their medications and maybe a ZIP code. Extract what is there, exactly as it appears. Return null for any field that is missing or illegible.
{
  "client_first_name": "",
  "client_last_name": "",
  "date_of_birth": "MM/DD/YYYY",
  "address": "",
  "city": "",
  "state": "",
  "zip_code": "",
  "phone_number": "",
  "soa_date": "MM/DD/YYYY",
  "appointment_date": "MM/DD/YYYY",
  "coverage_types": [],
  "drugs": [
    {
      "name": "",
      "dosage": "",
      "frequency": "",
      "is_injectable": false,
      "crossed_out": false
    }
  ],
  "providers": [
    {
      "raw_text": "",
      "last_name": "",
      "first_name": "",
      "specialty": "",
      "clinic_name": "",
      "city": ""
    }
  ],
  "custom_plans": "",
  "signature_present": true,
  "confidence": 0.0,
  "low_confidence_fields": [],
  "field_notes": [
    { "field": "", "note": "" }
  ],
  "flags": []
}
Neither the form nor an email has a signature, SOA date, appointment date or coverage-type section: return null for soa_date and appointment_date, [] for coverage_types and true for signature_present. Never flag their absence.
WHO THE CLIENT IS: the client is the person who takes the medications. In an email, that is usually the sender (look at the greeting, sign-off and "From" line). Never use the insurance agent's or the agency's name as the client: the person the email is addressed to (for example "Hi Lacey") is the agent, not the client. If someone writes on behalf of another person (for example a daughter writing about her mother), the client is the person taking the drugs; if you cannot tell who that is, still fill in your best reading of the name and add a field_notes entry on "client_first_name" saying so. If no client name appears anywhere, return null for both name fields with no note.
WHERE TO FIND THE ZIP: on the form it is the ZIP field. In an email, look anywhere: the body, a written address, or the signature block. If a full address is given, also fill in address, city and state.
NEVER GUESS OR COMPLETE VALUES. Write date_of_birth, zip_code, phone_number and every drug dosage exactly as written, including any "?", "or", or missing digits. If the ZIP is written with 4 digits, return those 4 digits. If a dose is written "10 or 25?", return "10 or 25?". If a digit is unreadable, put "?" in its place (for example "04/1?/1951"). Never invent a ZIP from a city name. The agency will fix these by hand; a confident guess is worse than an honest gap.
For drugs, set is_injectable to true for any injection, pen, insulin, or other injectable medication (for example Ozempic, Trulicity, Mounjaro, insulin pens). Set crossed_out to true if the drug line is struck through, scribbled over, or marked as stopped or discontinued (in an email: "I stopped taking", "no longer on"); still include the drug in the list. Write the drug name as written, even if it looks misspelled. If a strength and how often are written together (for example "metformin 500 twice a day"), put "500mg" in dosage and "twice a day" in frequency. Do not list vitamins or supplements unless they are clearly written as prescriptions.
For providers extract every doctor, dentist, specialist, or clinic mentioned anywhere. For each entry: raw_text is the exact text as written. Parse out last_name, first_name, specialty, clinic_name, and city where you can; leave any sub-field blank if it is not written. Do not invent providers. Pharmacies are not providers.
For custom_plans scan the entire page or message, including handwritten notes at the top, in margins, or any blank space. Look for requests to compare specific plans using phrases like "also check", "compare", "add", "include", "what about", "check". If you see any note that appears to reference a plan name — even if partially illegible — make your best attempt to extract it as a comma-separated string. Return an empty string "" only if there are absolutely no plan requests anywhere.
For field_notes, add one entry for EVERY field an agent should look at, using the exact field path in dot/bracket notation matching the JSON above ("zip_code", "date_of_birth", "phone_number", "drugs[3].name", "drugs[1].dosage", "providers[0].last_name"). "note" is one short plain-English sentence saying what was written and what to check. Add an entry when ANY of these is true:
- the handwriting or screenshot text was unclear or partially illegible;
- a drug line is crossed out or the client says they stopped it (use the drug's ".name" path);
- a drug has no dosage written (use its ".dosage" path);
- a dosage is ambiguous, has a "?" or "or", or looks unusually high or low for that drug;
- a drug name may be misspelled or could be a different drug;
- the ZIP code is missing entirely, incomplete, has the wrong number of digits, or has an unreadable digit (the ZIP is required to look up plans);
- a date of birth or phone number IS written but is incomplete or has an unreadable digit.
Do not add entries for fields that are simply not given: date of birth, phone, email, address, city, state, doctors, dentist, specialists and current plan are all optional. If there is nothing to note, return [].
For flags, include ONLY things an agent must act on before comparing plans, as short plain-English sentences that start with the drug or field name (for example "Metoprolol: no dosage written"):
- Medication concerns: a crossed-out or stopped drug, an unusual or likely wrong dose, a drug name you could not read with certainty, a brand/generic ambiguity, or a dosage missing for a listed drug.
- ZIP code: missing, illegible, incomplete or ambiguous.
- A client name or date of birth that IS written but is illegible or ambiguous.
Do NOT flag: optional fields that are not given (date of birth, address, city, state, phone, email, doctors, dentist, specialists, current plan), whether the input is the form, an email or a screenshot, its layout, missing signatures or dates, or text marking the document as a sample or test. If there is nothing to act on, return [].
For low_confidence_fields, list every field path that appears in field_notes. If field_notes is empty, return [].`;

const MAX_TOKENS = 8000;   // ceiling, not usage - Claude only uses what it needs

const IMAGE_TYPES = ['image/png', 'image/jpeg', 'image/gif', 'image/webp'];
const out = [];

for (let i = 0; i < $input.all().length; i++) {
  const item = $input.all()[i];
  const binary = item.binary || {};
  const key = binary.data ? 'data' : Object.keys(binary)[0];
  if (!key) {
    throw new Error('Build Bedrock Body: no file came in from Download PDF (no binary data). Check that Download PDF ran and output a file.');
  }

  const info = binary[key];
  const fileName = info.fileName || '(no name)';
  let mime = (info.mimeType || '').toLowerCase();
  if (!mime || mime === 'application/octet-stream') {
    const ext = fileName.toLowerCase().split('.').pop();
    mime = { pdf: 'application/pdf', png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg', gif: 'image/gif', webp: 'image/webp' }[ext] || mime;
  }

  const buffer = await this.helpers.getBinaryDataBuffer(i, key);
  if (!buffer || buffer.length === 0) {
    throw new Error(`Build Bedrock Body: the file "${fileName}" is empty.`);
  }
  const b64 = buffer.toString('base64');

  let fileBlock;
  if (mime === 'application/pdf') {
    fileBlock = { type: 'document', source: { type: 'base64', media_type: 'application/pdf', data: b64 } };
  } else if (IMAGE_TYPES.includes(mime)) {
    fileBlock = { type: 'image', source: { type: 'base64', media_type: mime, data: b64 } };
  } else {
    throw new Error(`Build Bedrock Body: "${fileName}" is type "${mime || 'unknown'}". Only PDF, PNG, JPG, GIF or WEBP can be read.`);
  }

  const body = {
    anthropic_version: 'bedrock-2023-05-31',
    max_tokens: MAX_TOKENS,
    temperature: 0,
    messages: [{ role: 'user', content: [fileBlock, { type: 'text', text: PROMPT }] }],
  };

  out.push({ json: { body: JSON.stringify(body), file_name: fileName, file_type: mime, file_kb: Math.round(buffer.length / 1024) } });
}

return out;
