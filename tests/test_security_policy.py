import unittest

from security_policy import valid_password


class PasswordPolicyTests(unittest.TestCase):
    def test_requires_twelve_to_256_characters_with_letters_and_digits(self):
        self.assertFalse(valid_password("Short123"))
        self.assertFalse(valid_password("lettersOnlyLong"))
        self.assertFalse(valid_password("123456789012"))
        self.assertTrue(valid_password("A sufficiently long 123"))
        self.assertFalse(valid_password("A" * 257 + "1"))

    def test_blocks_common_passwords_case_insensitively(self):
        self.assertFalse(valid_password("Password123456"))
        self.assertFalse(valid_password("Password123!"))
        self.assertTrue(valid_password("CorrectHorseBattery123!"))


if __name__ == "__main__":
    unittest.main()
