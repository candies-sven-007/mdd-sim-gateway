# SPDX-License-Identifier: GPL-2.0-only
#
# LICENSE EXCEPTION TO THE REPOSITORY DEFAULT. MDD Sim Gateway as a whole is GPL-3.0-only,
# but FIXED_FN below is a modified copy of Asterisk's res_pjsip_messaging parse_tpdu(), and
# Asterisk is GPL-2.0-only. A derivative of GPL-2.0-only code cannot be relicensed to
# GPL-3.0, so this file — and the modified Asterisk source it produces at build time — stay
# under GPL-2.0-only. See THIRD_PARTY_LICENSES.md.

import os
import sys

# PATCH_CONCAT_UDH: expose the concatenated-SMS segment triplet on an inbound (MT) SMS.
#
# A message longer than one SMS is split by the SMSC into several SMS-DELIVER PDUs, each
# carrying a User Data Header that says which part it is (3GPP TS 23.040 9.2.3.24.1/.8).
# Stock parse_tpdu() already unpacks that header — unpacksms() fills `udh`/`udhl` — but then
# drops it on the floor ("TODO: udh, scts") and sets the body to the fragment's own text. So
# every part surfaced as its own standalone message and the user saw one text arrive as N
# unordered pieces (the parts are not even guaranteed to arrive in order).
#
# Reassembly itself is NOT done here: it needs state across separate SIP transactions, and the
# RP-ACK for each part must go back immediately. Instead this exposes ref/total/seq as message
# variables, which the dialplan forwards to the manager (control/app/main.py), where the parts
# are buffered in SQLite and merged into a single message.
#
# The vars are set ONLY for a well-formed segment of a real multi-part message, so an ordinary
# single-part SMS leaves them empty and takes exactly the previous code path.
#
# PATCH_SMS_PDU_META: expose the PDU header fields too — TP-PID, TP-DCS, the UDH IE area and
# the raw PDU. Stock parse_tpdu() drops all of them, which makes a binary SMS (8-bit user data,
# or message class 2 addressed to the SIM) indistinguishable from a text: unpacksms() widens
# each payload byte to one character and the manager stores the result as a message body. Line 1
# collected 16 such payloads from short-codes 2942/2939, displayed to the user as mojibake. With
# these vars the manager can file a machine payload away instead (control/app/sms_pdu.py).

FIXED_FN = r'''static void parse_tpdu(struct ast_msg *msg, unsigned char *tpdu, int tpdu_len)
{
	/* PATCH_CONCAT_UDH */
	if (tpdu_len < 2)
	{
		return;
	}
	if (tpdu[0] & 3)
	{
		ast_log(LOG_WARNING, "Unhandled PDU type %x\n", tpdu[0] & 3);
		return;
	}
	/*int srr = ((tpdu[0] & 0x20) ? 1 : 0);*/
	int udhi = ((tpdu[0] & 0x40) ? 1 : 0);
	/*int rp = ((tpdu[0] & 0x80) ? 1 : 0);*/
	int p = 1;
	char oa[300];
	p += unpackaddress(oa, tpdu + p, sizeof(oa));
	if (p + 9 > tpdu_len)
		return;
	int pid = tpdu[p++];
	int dcs = tpdu[p++];
	struct timeval scts = unpackdate(tpdu + p);
	p += 7;

	/* PATCH_SMS_PDU_META: expose the header fields that say what KIND of message this is.
	 * TP-DCS distinguishes text from binary user data and carries the message class (3GPP
	 * TS 23.038 4); TP-PID marks a payload addressed to the SIM rather than the user
	 * (TS 23.040 9.2.3.9). Neither survives the decode below — the 8-bit path in unpacksms()
	 * widens each payload byte to one character, so a binary SMS reaches the manager looking
	 * exactly like a text that happens to be mojibake, and lands in the conversation view.
	 * SMS_TPDU_HEX carries the whole PDU verbatim so a payload we cannot classify yet can
	 * still be examined later as it was on the wire, not through a lossy decode. */
	char pdu_num[16];
	snprintf(pdu_num, sizeof(pdu_num), "%d", pid);
	ast_msg_set_var(msg, "SMS_TP_PID", pdu_num);
	snprintf(pdu_num, sizeof(pdu_num), "%d", dcs);
	ast_msg_set_var(msg, "SMS_TP_DCS", pdu_num);
	{
		/* An SMS-DELIVER tops out around 164 octets (header + 140 of user data); the cap is
		 * applied anyway so a bogus tpdu_len can never walk past the buffer. */
		char pdu_hex[2 * 200 + 1];
		int hn = tpdu_len > 200 ? 200 : (tpdu_len < 0 ? 0 : tpdu_len);
		int hi;
		for (hi = 0; hi < hn; hi++) {
			snprintf(pdu_hex + hi * 2, 3, "%02x", tpdu[hi]);
		}
		pdu_hex[hn * 2] = '\0';
		ast_msg_set_var(msg, "SMS_TPDU_HEX", pdu_hex);
	}
	unsigned short ud[300];
	/* Zero-initialised: on a malformed header unpacksms() can report a udhl longer than the
	 * bytes it actually copied, and the IE walk below would otherwise read uninitialised
	 * stack. Reading zeros instead just ends the walk without a match. */
	unsigned char udh[300] = { 0 };
	int udhl, udl;
	p += unpacksms(dcs, tpdu + p, udh, &udhl, ud, &udl, udhi);
	ud[udl] = 0;

	char buf2[300 * 4 + 5];
	utf16_to_utf8(ud, udl, buf2, sizeof(buf2));
	ast_log(LOG_DEBUG, "SMS UD='%s' OA='%s'.\n", buf2, oa);

	/* Concatenation IE. unpacksms() leaves `udh` holding the IE bytes AFTER the UDHL octet,
	 * and `udhl` is the length of that IE area. Walk the IEs looking for the concatenation
	 * one: IEI 0x00 = 8-bit reference (3 bytes: ref, total, seq), IEI 0x08 = 16-bit
	 * reference (4 bytes: ref-high, ref-low, total, seq). Any other IE (national language
	 * shift, port addressing, …) is skipped by its own length. */
	if (udhi && udhl > 0) {
		int k = 0;
		if (udhl > (int) sizeof(udh)) {
			udhl = (int) sizeof(udh);
		}
		{
			/* PATCH_SMS_PDU_META: the IE area verbatim. Port addressing (IEI 0x04/0x05) is
			 * what identifies WHICH service a binary payload belongs to — WAP push, SIM
			 * data download, a vendor's own port — and the walk below only looks for
			 * concatenation, so without this the routing information is lost. */
			char udh_hex[2 * sizeof(udh) + 1];
			int hi;
			for (hi = 0; hi < udhl; hi++) {
				snprintf(udh_hex + hi * 2, 3, "%02x", udh[hi]);
			}
			udh_hex[udhl * 2] = '\0';
			ast_msg_set_var(msg, "SMS_UDH_HEX", udh_hex);
		}
		while (k + 2 <= udhl) {
			int iei = udh[k];
			int iedl = udh[k + 1];
			unsigned char *ie = udh + k + 2;
			int ref = -1, total = 0, seq = 0;

			if (k + 2 + iedl > udhl) {
				break;                 /* truncated IE — nothing trustworthy follows */
			}
			if (iei == 0x00 && iedl == 3) {
				ref = ie[0];
				total = ie[1];
				seq = ie[2];
			} else if (iei == 0x08 && iedl == 4) {
				ref = (ie[0] << 8) | ie[1];
				total = ie[2];
				seq = ie[3];
			}
			/* A usable segment has total >= 2 and 1 <= seq <= total. Anything else is not a
			 * concatenation we can reassemble: leave the vars unset so the part is delivered
			 * on its own, exactly as before this patch. */
			if (ref >= 0 && total >= 2 && seq >= 1 && seq <= total) {
				char nbuf[16];
				snprintf(nbuf, sizeof(nbuf), "%d", ref);
				ast_msg_set_var(msg, "SMS_CONCAT_REF", nbuf);
				snprintf(nbuf, sizeof(nbuf), "%d", total);
				ast_msg_set_var(msg, "SMS_CONCAT_TOTAL", nbuf);
				snprintf(nbuf, sizeof(nbuf), "%d", seq);
				ast_msg_set_var(msg, "SMS_CONCAT_SEQ", nbuf);
				ast_log(LOG_DEBUG, "SMS concat ref=%d part %d/%d.\n", ref, seq, total);
				break;
			}
			k += 2 + iedl;
		}
	}

	char buf_scts[30];
	snprintf(buf_scts, sizeof(buf_scts), "%lld", (long long) scts.tv_sec);
	ast_msg_set_var(msg, "SMS_SMSC_TIMESTAMP", buf_scts);
	ast_msg_set_from(msg, "%s", oa);
	ast_msg_set_body(msg, "%s", buf2);
}'''

f = os.path.join(os.environ.get('AST_SRC', '/home/asterisk-build/asterisk'),
                 'res/res_pjsip_messaging.c')
s = open(f).read()
# Both markers: a tree carrying only the older concat-only patch still needs this run, and
# the replacement below finds it by the same signature and swaps it wholesale.
if 'PATCH_CONCAT_UDH' in s and 'PATCH_SMS_PDU_META' in s:
    print("already patched"); sys.exit(0)

start = s.find('static void parse_tpdu(struct ast_msg *msg, unsigned char *tpdu, int tpdu_len)')
if start < 0:
    print("PATTERN NOT FOUND: parse_tpdu signature"); sys.exit(1)
# brace-match to find the end of the function
i = s.find('{', start)
depth = 0
end = -1
for j in range(i, len(s)):
    if s[j] == '{': depth += 1
    elif s[j] == '}':
        depth -= 1
        if depth == 0:
            end = j + 1
            break
if end < 0:
    print("BRACE MATCH FAILED"); sys.exit(1)

s2 = s[:start] + FIXED_FN + s[end:]
open(f, 'w').write(s2)
print("patched OK (parse_tpdu replaced, %d -> %d bytes)" % (end - start, len(FIXED_FN)))
