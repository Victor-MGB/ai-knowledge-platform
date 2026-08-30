import * as bcrypt from "bcryptjs";

/** Password hashing with bcrypt. Cost factor is configurable (compiled in at
 * construction, never changed per-call), so a later security upgrade is a
 * bump + a re-hash pass, not silent behavior drift. */

export class PasswordService {
  constructor(private readonly rounds: number = 10) {}

  hash(plain: string): Promise<string> {
    return bcrypt.hash(plain, this.rounds);
  }

  verify(plain: string, hash: string): Promise<boolean> {
    return bcrypt.compare(plain, hash);
  }
}